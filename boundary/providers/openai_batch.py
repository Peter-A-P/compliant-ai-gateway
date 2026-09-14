"""The OpenAI-shaped Batch API, which Together also serves.

Verified against the current documentation on 2026-09-14:
    https://developers.openai.com/api/docs/guides/batch
    https://docs.together.ai/docs/batch-inference

Four round trips rather than Anthropic's three, because the requests go up as a file first:

    POST {base}/files                     multipart, purpose=batch  -> {"id": "file-..."}
    POST {base}/batches                   {"input_file_id", "endpoint", "completion_window"}
    GET  {base}/batches/{id}              -> {"status", "output_file_id", "request_counts"}
    GET  {base}/files/{output_file_id}/content  -> JSON Lines, one object per request

Each input line is a whole HTTP request:

    {"custom_id", "method": "POST", "url": "/v1/chat/completions", "body": {...}}

and each output line is that request's outcome:

    {"id", "custom_id", "response": {"status_code", "request_id", "body": {...}}, "error": null}

`body` is byte for byte the body a single call would have sent, so a prompt costs the same
request hash whether it was batched or not, exactly as on the Anthropic path.

Three things about this shape are worth stating because they are not obvious:

**Output order is not input order.** Both vendors say so. Nothing here depends on order: the
mapping is by `custom_id`, which the gateway sets to the ledger row's `call_uid`.

**The results are named by a file id, not by a URL.** Anthropic hands back a `results_url` and
the gateway checks that it points at the configured host before sending the key to it. There is
no vendor-supplied URL here to check, which removes that risk and adds a smaller one: the id is
interpolated into a path this code builds. So `_safe_file_id` refuses anything that is not a
plain id, and a vendor cannot walk the path or move the host.

**Together discounts two models and not the rest.** Its batch endpoint is OpenAI-shaped, but
only `meta-llama/Llama-3.3-70B-Instruct-Turbo` and `openai/whisper-large-v3` run at half price;
everything else runs at the standard rate. That is a price file fact rather than an adapter
fact, and it is why `openai/gpt-oss-120b` carries `batch_multiplier: 1.0` rather than 0.5: the
same batch call costs full price for that model, and saying 0.5 would understate the invoice.

Not every OpenAI-compatible host has this endpoint. Ollama does not, and a local server that
answered a batch create with an HTML 404 would be a confusing failure. So a provider entry has
to opt in with `batches: true`; the default for `openai_compat` is off. Anthropic and Google
default on, because their batch endpoints are part of the API rather than a compatibility
extra.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from boundary.config import ProviderConfig, ProviderKind
from boundary.errors import ProviderError
from boundary.providers._json import dumps, loads
from boundary.providers.base import (
    BatchItem,
    BatchItemResult,
    BatchProgress,
    BatchSubmitted,
    BuiltRequest,
    ParsedResponse,
)
from boundary.providers.openai_compat import OpenAICompatAdapter

FILES_PATH = "/files"
BATCHES_PATH = "/batches"
CHAT_COMPLETIONS_PATH = "/v1/chat/completions"

# 24h is the only window OpenAI documents and the one Together discounts most (50 percent
# against 25 percent for its 4h window). Nothing in this portfolio wants an answer sooner
# than a batch gives it, so the cheap window is the only one offered.
COMPLETION_WINDOW = "24h"

# Multipart needs a boundary that cannot appear in the payload. This one is fixed rather than
# random so that the same items build the same bytes twice, which is what lets a batch submit
# be compared against a golden and what a retry depends on.
_MULTIPART_BOUNDARY = "boundary-batch-Ku8Qw2Nf1Xz"

# The vendor's own words for a batch that will not change again.
_ENDED_STATES = frozenset({"completed", "failed", "expired", "cancelled", "canceled"})

# A file id as both vendors write it. Anything else is refused before it reaches a URL.
_FILE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _safe_file_id(provider_name: str, file_id: str) -> str:
    """A vendor-supplied id is about to become part of a path this code builds.

    It carries the API key, so an id containing a slash, a scheme or a host would be a way to
    send that key somewhere else. Refused here rather than sanitised, because there is no
    legitimate id this rejects.
    """
    if not _FILE_ID.match(file_id):
        raise ProviderError(
            provider_name,
            "batch_file_id",
            f"refusing a batch file id that is not a plain id: {file_id!r}",
        )
    return file_id


class OpenAICompatBatchAdapter(OpenAICompatAdapter):
    """Batches for OpenAI-shaped hosts. Inherits every single-call method unchanged."""

    kind: ClassVar[ProviderKind] = ProviderKind.OPENAI_COMPAT
    uploads_input_file: ClassVar[bool] = True

    # -- upload -------------------------------------------------------------------------

    def input_file_bytes(self, items: Sequence[BatchItem], provider: ProviderConfig) -> bytes:
        """The JSONL the vendor will read, one whole request per line."""
        lines: list[bytes] = []
        for custom_id, ref, request in items:
            body = self.chat_body(ref, request, provider)
            lines.append(
                dumps(
                    {
                        "custom_id": custom_id,
                        "method": "POST",
                        "url": CHAT_COMPLETIONS_PATH,
                        "body": body,
                    }
                )
            )
        return b"\n".join(lines) + b"\n"

    def build_batch_upload(
        self,
        items: Sequence[BatchItem],
        provider: ProviderConfig,
        api_key: str | None,
    ) -> BuiltRequest:
        payload = self.input_file_bytes(items, provider)
        b = _MULTIPART_BOUNDARY
        body = b"".join(
            [
                f"--{b}\r\n".encode(),
                b'Content-Disposition: form-data; name="purpose"\r\n\r\nbatch\r\n',
                f"--{b}\r\n".encode(),
                b'Content-Disposition: form-data; name="file"; filename="batch.jsonl"\r\n',
                b"Content-Type: application/jsonl\r\n\r\n",
                payload,
                f"\r\n--{b}--\r\n".encode(),
            ]
        )
        headers = {
            "content-type": f"multipart/form-data; boundary={b}",
            **provider.headers,
        }
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        return BuiltRequest(
            method="POST",
            url=provider.base_url.rstrip("/") + FILES_PATH,
            headers=headers,
            body=body,
        )

    def parse_batch_upload(self, status: int, headers: Mapping[str, str], body: bytes) -> str:
        raw = self._object(status, headers, body)
        file_id = raw.get("id")
        if not isinstance(file_id, str) or not file_id:
            raise ProviderError(
                "openai_compat", status, "file upload returned no id", headers=headers
            )
        return file_id

    # -- create -------------------------------------------------------------------------

    def build_batch_create(
        self, upload_id: str, provider: ProviderConfig, api_key: str | None
    ) -> BuiltRequest:
        body = {
            "input_file_id": upload_id,
            "endpoint": CHAT_COMPLETIONS_PATH,
            "completion_window": COMPLETION_WINDOW,
        }
        return BuiltRequest(
            method="POST",
            url=provider.base_url.rstrip("/") + BATCHES_PATH,
            headers=self._json_headers(provider, api_key),
            body=dumps(body),
        )

    def build_batch_submit(
        self,
        items: Sequence[BatchItem],
        provider: ProviderConfig,
        api_key: str | None,
    ) -> BuiltRequest:
        """Not the entry point for this shape: the gateway uploads, then creates.

        Present because BatchAdapter declares it. Reaching it means the gateway ignored
        `uploads_input_file`, which would send the requests nowhere useful, so it says so
        rather than building something plausible.
        """
        raise ProviderError(
            "openai_compat",
            "batch_submit",
            "this provider uploads its requests as a file first; the gateway must call "
            "build_batch_upload and then build_batch_create",
        )

    def parse_batch_submit(
        self, status: int, headers: Mapping[str, str], body: bytes
    ) -> BatchSubmitted:
        raw = self._object(status, headers, body)
        batch_id = raw.get("id")
        if not isinstance(batch_id, str) or not batch_id:
            raise ProviderError(
                "openai_compat", status, "batch create returned no id", headers=headers
            )
        return BatchSubmitted(
            batch_id=batch_id,
            processing_status=str(raw.get("status", "validating")),
            raw=raw,
        )

    # -- status -------------------------------------------------------------------------

    def build_batch_status(
        self, batch_id: str, provider: ProviderConfig, api_key: str | None
    ) -> BuiltRequest:
        safe = _safe_file_id("openai_compat", batch_id)
        return BuiltRequest(
            method="GET",
            url=f"{provider.base_url.rstrip('/')}{BATCHES_PATH}/{safe}",
            headers=self._json_headers(provider, api_key),
            body=b"",
        )

    def parse_batch_status(
        self, status: int, headers: Mapping[str, str], body: bytes
    ) -> BatchProgress:
        raw = self._object(status, headers, body)
        state = str(raw.get("status", ""))
        counts_raw = raw.get("request_counts")
        counts: dict[str, int] = {}
        if isinstance(counts_raw, dict):
            counts = {k: int(v) for k, v in counts_raw.items() if isinstance(v, int)}
        out_id = raw.get("output_file_id")
        # `results_url` carries the output file id, not a URL: there is no vendor-supplied
        # URL in this shape, and build_batch_results turns the id into a path on the
        # configured host. A batch that failed outright has no output file and ends without
        # one, which the gateway reports rather than silently completing rows.
        return BatchProgress(
            batch_id=str(raw.get("id", "")),
            processing_status=state,
            ended=state.lower() in _ENDED_STATES,
            results_url=out_id if isinstance(out_id, str) and out_id else None,
            counts=counts,
            raw=raw,
        )

    # -- results ------------------------------------------------------------------------

    def build_batch_results(
        self, results_url: str, provider: ProviderConfig, api_key: str | None
    ) -> BuiltRequest:
        safe = _safe_file_id("openai_compat", results_url)
        return BuiltRequest(
            method="GET",
            url=f"{provider.base_url.rstrip('/')}{FILES_PATH}/{safe}/content",
            headers=self._json_headers(provider, api_key),
            body=b"",
        )

    def parse_batch_results(
        self, status: int, headers: Mapping[str, str], body: bytes
    ) -> list[BatchItemResult]:
        if not 200 <= status < 300:
            raise ProviderError(
                "openai_compat", status, body.decode("utf-8", "replace")[:500], headers=headers
            )
        out: list[BatchItemResult] = []
        for line in body.splitlines():
            if not line.strip():
                continue
            try:
                rec = loads(line)
            except ValueError as e:
                raise ProviderError(
                    "openai_compat", status, "batch results line is not JSON", headers=headers
                ) from e
            if not isinstance(rec, dict):
                continue
            custom_id = str(rec.get("custom_id", ""))
            err = rec.get("error")
            response = rec.get("response")
            if err is not None or not isinstance(response, dict):
                out.append(
                    BatchItemResult(
                        custom_id=custom_id,
                        outcome="errored",
                        error=_error_text(err) or "no response",
                    )
                )
                continue
            code = response.get("status_code")
            inner = response.get("body")
            if not isinstance(code, int) or not 200 <= code < 300 or not isinstance(inner, dict):
                out.append(
                    BatchItemResult(
                        custom_id=custom_id,
                        outcome="errored",
                        error=f"http {code}",
                    )
                )
                continue
            out.append(
                BatchItemResult(
                    custom_id=custom_id,
                    outcome="succeeded",
                    parsed=self.parse_completion(inner),
                )
            )
        return out

    # -- shared -------------------------------------------------------------------------

    def _json_headers(self, provider: ProviderConfig, api_key: str | None) -> dict[str, str]:
        headers: dict[str, str] = {"content-type": "application/json", **provider.headers}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        return headers

    def _object(
        self, status: int, headers: Mapping[str, str], body: bytes
    ) -> dict[str, Any]:
        if not 200 <= status < 300:
            raise ProviderError(
                "openai_compat", status, body.decode("utf-8", "replace")[:500], headers=headers
            )
        try:
            raw = loads(body)
        except ValueError as e:
            raise ProviderError(
                "openai_compat", status, body.decode("utf-8", "replace")[:500], headers=headers
            ) from e
        if not isinstance(raw, dict):
            raise ProviderError(
                "openai_compat", status, "expected a JSON object", headers=headers
            )
        return raw


def _error_text(err: object) -> str | None:
    if err is None:
        return None
    if isinstance(err, dict):
        message = err.get("message")
        code = err.get("code")
        if isinstance(message, str):
            return f"{code}: {message}" if code else message
    return str(err)


__all__ = ["OpenAICompatBatchAdapter", "ParsedResponse"]
