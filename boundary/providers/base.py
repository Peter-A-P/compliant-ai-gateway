"""The Adapter protocol every provider implements.

An adapter is pure: it turns a ChatRequest into bytes and a URL, and turns status, headers
and bytes back into a ParsedResponse. It never performs I/O, so it can be tested against
golden requests and responses without a network, and so the transport can assert that
the bytes it sends are exactly the bytes the adapter built.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

from boundary.config import ProviderConfig, ProviderKind
from boundary.errors import ProviderError
from boundary.routes import ModelRef
from boundary.types import ChatRequest, Usage


@dataclass(frozen=True, slots=True)
class BuiltRequest:
    """Exactly what will leave the process. `body` is bytes, not a dict, so that the
    pass-through byte-equality test compares what was built with what was sent."""

    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes

    def redacted_headers(self) -> dict[str, str]:
        """Headers safe to store: credential-bearing values replaced with a marker."""
        out: dict[str, str] = {}
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in _SECRET_HEADERS or "key" in lk or "token" in lk:
                out[k] = "<redacted>"
            else:
                out[k] = v
        return out


_SECRET_HEADERS = frozenset({"authorization", "x-api-key", "x-goog-api-key", "api-key"})


@dataclass(frozen=True, slots=True)
class ParsedResponse:
    """The vendor-neutral reading of a successful response body."""

    text: str | None
    finish_reason: str | None
    model_returned: str | None
    usage: Usage
    raw: Any = field(default=None)


class Adapter(Protocol):
    """Build a request, parse a response. No I/O.

    kind: which ProviderKind this adapter serves. One adapter can serve many provider
        entries (every OpenAI-compatible host uses the same adapter).
    build_request: the exact bytes and headers for the request. Called once per attempt;
        must be deterministic for the same inputs so retries send identical bytes.
    parse_response: called for 2xx responses. Raises ProviderError when the body is not
        the shape the vendor documents, so a malformed success is not mistaken for one.
    parse_usage: token counts from a parsed body, or Usage() when absent. Used by the raw
        escape hatch as well, where the body shape may only partly be known.
    parse_error: turn a non-2xx response into a ProviderError with the vendor's message.
    """

    kind: ClassVar[ProviderKind]

    def build_request(
        self,
        ref: ModelRef,
        request: ChatRequest,
        provider: ProviderConfig,
        api_key: str | None,
    ) -> BuiltRequest: ...

    def parse_response(
        self,
        status: int,
        headers: Mapping[str, str],
        body: bytes,
    ) -> ParsedResponse: ...

    def parse_usage(self, raw: Any) -> Usage: ...

    def parse_error(
        self,
        provider_name: str,
        status: int,
        headers: Mapping[str, str],
        body: bytes,
    ) -> ProviderError: ...


# -- streaming (0.3) ------------------------------------------------------------------------


class StreamParser(Protocol):
    """Reads the data payloads of one streamed response, one event at a time.

    Stateful by necessity, and made fresh for every attempt by `StreamingAdapter.stream_parser`,
    so a retried stream cannot inherit half of the previous one's text.

    feed: read one event's data payload. Returns True when the event carried a content
        delta, which is what the gateway stamps as time to first token. The sentinel a host
        sends to close the stream is the parser's to recognise and ignore.
    usage_seen: whether any event carried a usage object. A host that ignores the request
        for usage streams text and no counts, and a row costed from no counts would be a
        cost of zero wearing a real number's clothes, so the gateway writes such a row
        uncosted. This is how it knows.
    result: the response assembled from every event fed so far, in the same shape a
        non-streamed call would have parsed. Raises ProviderError when nothing usable
        arrived, so an empty 2xx stream is a malformed response rather than a success.
    drain: the content text fed since the last drain (0.13), so the gateway can hand a
        caller the text as it arrives. Every piece drained, joined, is the text `result`
        assembles: nothing is drained that the result leaves out, and nothing twice.
    """

    usage_seen: bool

    def feed(self, data: str) -> bool: ...

    def drain(self) -> str: ...

    def result(self) -> ParsedResponse: ...


class StreamingAdapter(Protocol):
    """An adapter kind that can stream a chat completion. No I/O, like Adapter.

    build_stream_request: the exact bytes of the streaming request. The body asks the
        host for a stream and for usage in the final event, on top of whatever the
        non-streaming body would have carried; nothing in the caller's request is dropped.
    stream_parser: a fresh StreamParser for one attempt.
    """

    kind: ClassVar[ProviderKind]

    def build_stream_request(
        self,
        ref: ModelRef,
        request: ChatRequest,
        provider: ProviderConfig,
        api_key: str | None,
    ) -> BuiltRequest: ...

    def stream_parser(self) -> StreamParser: ...

    def parse_error(
        self,
        provider_name: str,
        status: int,
        headers: Mapping[str, str],
        body: bytes,
    ) -> ProviderError: ...


# -- batches ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BatchSubmitted:
    """What the vendor said when it accepted a batch."""

    batch_id: str
    processing_status: str
    raw: Any = field(default=None)


@dataclass(frozen=True, slots=True)
class BatchProgress:
    """Where a batch has got to. `ended` is the only thing the gateway decides on; the
    vendor's own word for it is kept beside it for the error message."""

    batch_id: str
    processing_status: str
    ended: bool
    results_url: str | None
    counts: Mapping[str, int]
    raw: Any = field(default=None)


@dataclass(frozen=True, slots=True)
class BatchItemResult:
    """One request's outcome inside a batch, matched back by `custom_id`.

    outcome is the vendor's word: succeeded, errored, canceled or expired. Only a
    succeeded item has a parsed response, and only it is billed for output.
    """

    custom_id: str
    outcome: str
    parsed: ParsedResponse | None = field(default=None)
    error: str | None = field(default=None)

    @property
    def succeeded(self) -> bool:
        return self.outcome == "succeeded"


# One item of a batch as it is handed to an adapter: the custom_id that will identify it in
# the results, the model it goes to, and the request itself.
BatchItem = tuple[str, ModelRef, ChatRequest]


class BatchAdapter(Protocol):
    """The batch endpoints, for providers that have them.

    Separate from Adapter because only some providers offer batches. A provider without
    them should fail with a clear message naming the provider, rather than carry three
    methods that raise.

    The gateway sets `custom_id` to the ledger row's `call_uid`, so a result maps back to
    exactly one row, in any process, however the vendor orders the results file.

    uploads_input_file: whether the requests have to be uploaded as a file before a batch
        can be created. False for the vendors that take the requests inline (Anthropic,
        Google); True for the OpenAI-shaped ones, where a batch names a file id and the
        file is a separate POST. An adapter that sets it True must also satisfy
        UploadingBatchAdapter.
    """

    uploads_input_file: ClassVar[bool]

    def build_batch_submit(
        self,
        items: Sequence[BatchItem],
        provider: ProviderConfig,
        api_key: str | None,
    ) -> BuiltRequest: ...

    def parse_batch_submit(
        self, status: int, headers: Mapping[str, str], body: bytes
    ) -> BatchSubmitted: ...

    def build_batch_status(
        self, batch_id: str, provider: ProviderConfig, api_key: str | None
    ) -> BuiltRequest: ...

    def parse_batch_status(
        self, status: int, headers: Mapping[str, str], body: bytes
    ) -> BatchProgress: ...

    def build_batch_results(
        self, results_url: str, provider: ProviderConfig, api_key: str | None
    ) -> BuiltRequest: ...

    def parse_batch_results(
        self, status: int, headers: Mapping[str, str], body: bytes
    ) -> list[BatchItemResult]: ...


class UploadingBatchAdapter(BatchAdapter, Protocol):
    """A batch whose requests go up as a file first, then are named by a create call.

    Two round trips before the batch exists, which matters to the ledger rather than to the
    caller: the prompts leave the process during the *upload*, so the rows are written before
    that request and not before the create that follows it.

    Neither round trip is billed. A failure in either one completes every row as a failure
    with no cost, which is the honest record: the vendor has not accepted work it can charge
    for until the create succeeds.
    """

    def build_batch_upload(
        self,
        items: Sequence[BatchItem],
        provider: ProviderConfig,
        api_key: str | None,
    ) -> BuiltRequest: ...

    def parse_batch_upload(self, status: int, headers: Mapping[str, str], body: bytes) -> str:
        """The uploaded file's id, which the create call names."""
        ...

    def build_batch_create(
        self, upload_id: str, provider: ProviderConfig, api_key: str | None
    ) -> BuiltRequest: ...
