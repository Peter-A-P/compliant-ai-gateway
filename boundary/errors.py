"""Errors raised by boundary. All inherit from BoundaryError.

Every error carries the numbers or identifiers a caller needs to act, so that a refused
call can be understood without reading a log.
"""

from __future__ import annotations

from collections.abc import Mapping


class BoundaryError(Exception):
    """Base class for every error boundary raises."""


class ConfigError(BoundaryError):
    """A configuration file is missing, malformed, or internally inconsistent."""


class UnknownAlias(BoundaryError):
    """An alias was requested that the routes file does not define."""

    def __init__(self, alias: str, known: Mapping[str, object]) -> None:
        self.alias = alias
        self.known = sorted(known)
        super().__init__(
            f"unknown alias {alias!r}; routes file defines: {', '.join(self.known) or 'none'}"
        )


class UnknownPrice(BoundaryError):
    """No price is known for the model the vendor returned.

    Raised only when the caller asked for strict costing. By default an unknown price
    writes an uncosted ledger row and never raises; the library never guesses a price.
    """

    def __init__(self, provider: str, model: str, price_list: str) -> None:
        self.provider = provider
        self.model = model
        self.price_list = price_list
        super().__init__(f"no price for {provider}/{model} in price list {price_list}")


class SpendCapExceeded(BoundaryError):
    """The pre-call estimate would take spend past a cap. No request was made."""

    def __init__(
        self,
        scope: str,
        cap_usd: float,
        spent_usd: float,
        estimate_usd: float,
    ) -> None:
        self.scope = scope
        self.cap_usd = cap_usd
        self.spent_usd = spent_usd
        self.estimate_usd = estimate_usd
        super().__init__(
            f"spend cap {scope!r} of US${cap_usd:.2f} would be exceeded: "
            f"US${spent_usd:.4f} spent plus an estimated US${estimate_usd:.4f} for this call. "
            "No request was made. Raise the cap deliberately in caps.yaml; never bypass it in code."
        )


class PassthroughViolation(BoundaryError):
    """Something incompatible with pass-through mode was requested.

    Pass-through means: explicit provider and model identifier, no alias, no cache, no
    retries, no defaults filled in. Any of those would make the library a confound in a
    measurement, so the call is refused before anything leaves the process.
    """


class ProviderError(BoundaryError):
    """The vendor returned an error status, or the transport failed.

    In pass-through mode this is a result and is recorded as one. In standard mode it is
    raised after the retry policy is exhausted.
    """

    def __init__(
        self,
        provider: str,
        status: int | str,
        body: str,
        retries: int = 0,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.provider = provider
        self.status = status
        self.body = body
        self.retries = retries
        self.headers: Mapping[str, str] = dict(headers or {})
        short = body if len(body) <= 500 else body[:500] + "..."
        super().__init__(f"{provider} returned {status} after {retries} retries: {short}")
