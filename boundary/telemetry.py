"""One OpenTelemetry span per call, with no content. PLAN.md section 2.7.

Attributes follow the OpenTelemetry generative-AI semantic conventions where one exists
(gen_ai.*) and use a boundary.* prefix otherwise. Prompts and completions are never
attached. Exporters: none (default), console; OTLP arrives with Part B.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import IO

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.trace import Span, SpanKind, StatusCode

from boundary.config import TelemetryConfig
from boundary.errors import ConfigError

# The only attribute keys a span may carry. A test asserts nothing else is set, which is
# how "no content in telemetry" is enforced rather than promised.
ALLOWED_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "gen_ai.operation.name",
        "gen_ai.system",
        "gen_ai.request.model",
        "gen_ai.response.model",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.response.finish_reasons",
        "http.response.status_code",
        "error.type",
        "boundary.env",
        "boundary.project",
        "boundary.purpose",
        "boundary.run_id",
        "boundary.mode",
        "boundary.alias",
        "boundary.provider",
        "boundary.cost_usd",
        "boundary.costed",
        "boundary.cached",
        "boundary.retries",
        "boundary.latency_ms",
        "boundary.ledger_id",
        "boundary.version",
    }
)


@dataclass(frozen=True, slots=True)
class SpanIds:
    trace_id: str | None
    span_id: str | None


class Telemetry:
    def __init__(
        self, config: TelemetryConfig, *, version: str, out: IO[str] | None = None
    ) -> None:
        self.enabled = config.exporter != "none"
        self._provider: TracerProvider | None = None
        self._tracer: trace.Tracer
        if config.exporter == "none":
            self._tracer = trace.NoOpTracer()
        elif config.exporter == "console":
            self._provider = TracerProvider(
                resource=Resource.create({"service.name": "boundary", "service.version": version})
            )
            # The stream is resolved here, not at import time, so a redirected stdout is honoured.
            self._provider.add_span_processor(
                SimpleSpanProcessor(ConsoleSpanExporter(out=out or sys.stdout))
            )
            self._tracer = self._provider.get_tracer("boundary", version)
        else:
            raise ConfigError(
                "telemetry exporter 'otlp' arrives with Part B (May 2027); use 'none' or 'console'"
            )

    def start(self, name: str) -> Span:
        """Start a client span the caller ends with span.end()."""
        return self._tracer.start_span(name, kind=SpanKind.CLIENT)

    @contextmanager
    def span(self, name: str) -> Iterator[Span]:
        with self._tracer.start_as_current_span(name, kind=SpanKind.CLIENT) as s:
            yield s

    @staticmethod
    def ids(span: Span) -> SpanIds:
        ctx = span.get_span_context()
        if not ctx.is_valid:
            return SpanIds(None, None)
        return SpanIds(format(ctx.trace_id, "032x"), format(ctx.span_id, "016x"))

    @staticmethod
    def set_attributes(span: Span, attrs: dict[str, str | int | float | bool | None]) -> None:
        for k, v in attrs.items():
            if k not in ALLOWED_ATTRIBUTES:
                raise ValueError(
                    f"span attribute {k!r} is not in the allow list; no content in telemetry"
                )
            if v is not None:
                span.set_attribute(k, v)

    @staticmethod
    def mark_error(span: Span, error_type: str) -> None:
        span.set_status(StatusCode.ERROR, error_type)
        span.set_attribute("error.type", error_type)

    def shutdown(self) -> None:
        if self._provider is not None:
            self._provider.shutdown()
