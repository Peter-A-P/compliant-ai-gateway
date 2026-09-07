"""The Adapter protocol every provider implements.

An adapter is pure: it turns a ChatRequest into bytes and a URL, and turns status, headers
and bytes back into a ParsedResponse. It never performs I/O, so it can be tested against
golden requests and responses without a network, and so the transport can assert that
the bytes it sends are exactly the bytes the adapter built.
"""

from __future__ import annotations

from collections.abc import Mapping
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
