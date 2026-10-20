"""Decision models over raw HTTP: TypeSafe's System One interface (1.5).

A decision model takes a state and typed questions and returns a probability for every
option it was offered. It writes no text. Jev is the first (TypeSafe AI, September 2026),
served by OpenRouter at `/api/alpha/decisions`; Kev is an open-weights recreation on Qwen
that serves the same interface locally at `/v1/systemone`. One adapter serves both, and a
provider entry's `decisions_path` says where.

Request shape (POST {base_url}{decisions_path}):
    {"model", "state", "questions": {id: {"type", "instructions"?, "criteria"?}}, ...extra}
Response shape:
    {"model", "answers": {id: {"type", "choice"?, "score"?, "noul"?, "probabilities"?,
     "confidence"?}}, "usage": {"input_tokens", "output_tokens", "cost"?}, "id"?,
     "provider"?}
Error shapes:
    OpenRouter: {"error": {"message", "code"}}
    TypeSafe and Kev: HTTP 422 with {"detail": [{"loc", "msg", "type"}]}

**How a ChatRequest becomes a decision.** The gateway's call path, its caps, its ledger row,
its retries and its telemetry, all take a ChatRequest, and a decision is sent through the
same path rather than a parallel one. So the request's one user message is the state,
verbatim (a string, a mapping or a list of parts, as the interface takes), and
`extra["questions"]` are the questions. Anything the interface has no place for is refused
rather than dropped: a system prompt, a temperature, stop sequences, more than one message.
`max_tokens` is accepted and not sent, because the gateway fills a default in standard mode
and the interface has no such field; output is priced at zero for Jev, so the pre-call
estimate it feeds is the input alone.

**What comes back.** `ChatResponse.text` is the `answers` object as canonical JSON, so a
caller reads the probabilities with `json.loads(response.text)` and the gateway needs no
new response type. The usage is the host's `input_tokens` and `output_tokens`. OpenRouter
also reports a `cost`; it is ignored, because cost comes from a dated price file and the
returned usage and nothing else (CLAUDE.md), and it is what `docs/invoice-check.md` would
compare the ledger against.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, ClassVar

from boundary.config import ProviderConfig, ProviderKind
from boundary.errors import ProviderError
from boundary.providers._json import dumps, loads
from boundary.providers.base import BuiltRequest, ParsedResponse
from boundary.routes import ModelRef
from boundary.types import ChatRequest, Usage


class DecisionsAdapter:
    kind: ClassVar[ProviderKind] = ProviderKind.DECISIONS

    def decision_body(self, ref: ModelRef, request: ChatRequest) -> dict[str, Any]:
        """The decision request body, or ValueError for anything it cannot carry."""
        if request.system is not None:
            raise ValueError(
                "a decision request has no system prompt: put the instructions in each "
                "question's `instructions`"
            )
        if request.temperature is not None or request.stop:
            raise ValueError("a decision request takes no temperature or stop sequences")
        if len(request.messages) != 1 or request.messages[0].get("role") != "user":
            raise ValueError(
                "a decision request carries exactly one user message, which is the state"
            )
        questions = request.extra.get("questions")
        if not isinstance(questions, Mapping) or not questions:
            raise ValueError(
                "a decision request needs extra['questions']: a mapping of question id to "
                "{'type', 'instructions'?, 'criteria'?}"
            )
        body: dict[str, Any] = {
            "model": ref.model,
            "state": request.messages[0]["content"],
        }
        body.update(request.extra)
        return body

    def build_request(
        self,
        ref: ModelRef,
        request: ChatRequest,
        provider: ProviderConfig,
        api_key: str | None,
    ) -> BuiltRequest:
        headers: dict[str, str] = {"content-type": "application/json", **provider.headers}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        return BuiltRequest(
            method="POST",
            url=provider.base_url.rstrip("/") + provider.decisions_path,
            headers=headers,
            body=dumps(self.decision_body(ref, request)),
        )

    def parse_response(
        self, status: int, headers: Mapping[str, str], body: bytes
    ) -> ParsedResponse:
        try:
            raw = loads(body)
        except ValueError as e:
            raise ProviderError(
                "decisions", status, body.decode("utf-8", "replace"), headers=headers
            ) from e
        if not isinstance(raw, dict) or not isinstance(raw.get("answers"), dict):
            raise ProviderError(
                "decisions", status, body.decode("utf-8", "replace")[:500], headers=headers
            )
        return ParsedResponse(
            text=json.dumps(raw["answers"], sort_keys=True, ensure_ascii=False),
            finish_reason="stop",
            model_returned=raw.get("model") if isinstance(raw.get("model"), str) else None,
            usage=self.parse_usage(raw),
            raw=raw,
        )

    def parse_usage(self, raw: Any) -> Usage:
        if not isinstance(raw, dict):
            return Usage()
        u = raw.get("usage")
        if not isinstance(u, dict):
            return Usage()
        return Usage(
            input_tokens=int(u.get("input_tokens") or 0),
            output_tokens=int(u.get("output_tokens") or 0),
        )

    def parse_error(
        self, provider_name: str, status: int, headers: Mapping[str, str], body: bytes
    ) -> ProviderError:
        text = body.decode("utf-8", "replace")
        try:
            raw = loads(body)
        except ValueError:
            return ProviderError(provider_name, status, text, headers=headers)
        if isinstance(raw, dict):
            err = raw.get("error")
            detail = raw.get("detail")
            if isinstance(err, dict):
                text = f"{err.get('code') or 'error'}: {err.get('message', '')}"
            elif isinstance(detail, list):
                text = "; ".join(
                    f"{'.'.join(str(p) for p in d.get('loc', []))}: {d.get('msg', '')}"
                    for d in detail
                    if isinstance(d, dict)
                )
        return ProviderError(provider_name, status, text, headers=headers)
