"""Provider adapters. Each builds a request body itself and parses the response itself,
over raw HTTP with pinned API version headers. No vendor SDKs (PLAN.md section 2.2)."""

from boundary.providers.base import Adapter, BuiltRequest, ParsedResponse

__all__ = ["Adapter", "BuiltRequest", "ParsedResponse"]
