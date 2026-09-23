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


class BatchNotReady(BoundaryError):
    """Results were asked for before the vendor finished the batch.

    Not a failure: a batch is submitted in one process and collected later, often hours
    later, so "not yet" is the ordinary answer. The processing status and the vendor's
    own counts are carried so a caller can decide whether to wait or come back.
    """

    def __init__(
        self,
        batch_id: str,
        processing_status: str,
        counts: Mapping[str, int] | None = None,
    ) -> None:
        self.batch_id = batch_id
        self.processing_status = processing_status
        self.counts: Mapping[str, int] = dict(counts or {})
        detail = ", ".join(f"{k} {v}" for k, v in sorted(self.counts.items()))
        super().__init__(
            f"batch {batch_id} is {processing_status}, not ended"
            + (f" ({detail})" if detail else "")
            + ". Its ledger rows stay in flight at their estimate until it ends."
        )


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
        super().__init__(self._message())

    def _message(self) -> str:
        short = self.body if len(self.body) <= 500 else self.body[:500] + "..."
        return f"{self.provider} returned {self.status} after {self.retries} retries: {short}"

    def __str__(self) -> str:
        """Built from the current fields rather than frozen at construction.

        An adapter's `parse_error` does not know how many attempts were made, so it builds
        this error with the default of zero and the gateway, which does know, assigns
        `retries` afterwards. Formatting the message in `__init__` meant that assignment
        never reached the text: a Vertex 429 on 2026-09-16 was retried three times, the
        ledger recorded `retries = 3`, and the message said "after 0 retries".

        That is worse than cosmetic. The message is what a person reads first when a call
        fails, and reading "0 retries" is evidence that the retry policy did not run. It
        sent this project looking at the retry loop, which was working correctly.
        """
        return self._message()


class PolicyRefused(BoundaryError):
    """The data policy forbids this class of data from reaching this provider (0.12).

    Nothing was sent, and a ledger row was written with `error_type = 'policy_refused'`, so
    an attempted violation is on the record rather than only in the caller's exception log.
    `ledger_id` is that row. PLAN.md B2.2: violations are refused with a reason and audited.
    """

    def __init__(self, data_class: str, provider: str, reason: str, ledger_id: int) -> None:
        self.data_class = data_class
        self.provider = provider
        self.reason = reason
        self.ledger_id = ledger_id
        super().__init__(f"refused by the data policy: {reason} (ledger row {ledger_id})")
