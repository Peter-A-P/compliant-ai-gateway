"""Request and response types. Part of the frozen interface (docs/interface.md).

These are plain frozen dataclasses rather than pydantic models so that a caller pays no
validation cost per call beyond the few checks in __post_init__, and so that the request
the caller built is exactly the request the adapter serialises.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class Mode(StrEnum):
    """How a call is treated. See PLAN.md section 2.3.

    STANDARD: retries on 429, 5xx and timeouts; optional exact-match development cache;
        aliases resolved and defaults filled in. For development and normal use.
    PASSTHROUGH: no retries, no cache, no rewriting; explicit provider and model only;
        request and response bytes and headers written to a caller-owned raw store. For
        anything that is a measurement, starting with the 03 drift runs.
    """

    STANDARD = "standard"
    PASSTHROUGH = "passthrough"


_EMPTY: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """One chat completion request, vendor-neutral.

    model: either an alias from the routes file (standard mode only) or the explicit form
        "provider/model-id", for example "anthropic/claude-haiku-4-5-20251001". The
        provider part is a key in the providers section of boundary.yaml.
    messages: a sequence of {"role": ..., "content": ...} mappings. Content may be a string
        or a vendor-shaped list of blocks; blocks are passed through untouched.
    system: the system prompt, or None. Placed where the vendor expects it.
    max_tokens: required in pass-through mode (filling a default would be rewriting); in
        standard mode None means the configured default.
    temperature, stop: passed through when set.
    extra: vendor-specific fields merged into the request body verbatim, last, so that a
        caller can reach a feature the library does not model. In pass-through mode the
        adapter still builds the body; extra is part of what the caller built.
    """

    model: str
    messages: Sequence[Mapping[str, Any]]
    system: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    stop: Sequence[str] | None = None
    extra: Mapping[str, Any] = field(default_factory=lambda: _EMPTY)

    def __post_init__(self) -> None:
        if not self.model or self.model.strip() != self.model:
            raise ValueError("model must be a non-empty identifier without surrounding whitespace")
        if len(self.messages) == 0:
            raise ValueError("messages must contain at least one message")
        for i, m in enumerate(self.messages):
            if "role" not in m or "content" not in m:
                raise ValueError(f"messages[{i}] must have 'role' and 'content'")
        if self.max_tokens is not None and self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive when given")
        if self.temperature is not None and not (0.0 <= self.temperature <= 2.0):
            raise ValueError("temperature must be between 0.0 and 2.0 when given")

    @property
    def is_explicit(self) -> bool:
        """True when model is in the explicit "provider/model-id" form."""
        return "/" in self.model


@dataclass(frozen=True, slots=True)
class Usage:
    """Token counts as the vendor returned them. Never estimated here."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


@dataclass(frozen=True, slots=True)
class ChatResponse:
    """What a call returned, plus the ledger row it wrote.

    text: the first text block, or None on error.
    finish_reason: the vendor's reason, normalised to lower case, or None.
    usage: token counts from the vendor.
    cost_usd: computed from usage and the dated price list; None when costed is False.
    costed: False when the returned model identifier has no price in the price list.
    model_requested: the explicit "provider/model-id" after alias resolution.
    model_returned: the identifier the vendor reported, or None if it did not.
    provider: the providers key the call went to.
    latency_ms: wall time of the upstream call, retries included.
    status: HTTP status code, or the error type name when no response arrived.
    headers: response headers, kept in full for the drift record.
    raw: the parsed response body, or None when it could not be parsed.
    ledger_id: the row id, so a caller can join its own records to the ledger.
    mode: the mode the call ran in.
    retries: how many retries were made (always 0 in pass-through mode).
    cached: True when the development cache answered (never in pass-through mode).
    price_list: the price list version the row was costed with, or None.
    trace_id: the OpenTelemetry trace id in hex, or None when telemetry is off.
    """

    text: str | None
    finish_reason: str | None
    usage: Usage
    cost_usd: float | None
    costed: bool
    model_requested: str
    model_returned: str | None
    provider: str
    latency_ms: float
    status: int | str
    headers: Mapping[str, str]
    raw: Any
    ledger_id: int
    mode: Mode
    retries: int = 0
    cached: bool = False
    price_list: str | None = None
    trace_id: str | None = None

    @property
    def ok(self) -> bool:
        """True when the vendor answered with a 2xx status."""
        return isinstance(self.status, int) and 200 <= self.status < 300
