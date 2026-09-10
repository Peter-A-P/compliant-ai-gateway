"""Cost arithmetic. From returned usage and a price entry, never from a guess.

A None result means the call cannot be costed with this price entry: a cache or batch rate
is needed and the entry does not have it. The caller writes an uncosted row.
"""

from __future__ import annotations

from boundary.config import PriceEntry
from boundary.types import Usage

PER = 1_000_000.0

# Characters per token used for the pre-call estimate. Deliberately low (real text runs
# closer to 4), so the estimate is pessimistic: a refused call is cheap, an overspend is not.
ESTIMATE_CHARS_PER_TOKEN = 3.5


def cost_usd(usage: Usage, entry: PriceEntry, *, batch: bool = False) -> float | None:
    total = usage.input_tokens / PER * entry.input + usage.output_tokens / PER * entry.output
    if usage.cache_read_tokens:
        if entry.cache_read is None:
            return None
        total += usage.cache_read_tokens / PER * entry.cache_read
    if usage.cache_write_tokens:
        if entry.cache_write is None:
            return None
        total += usage.cache_write_tokens / PER * entry.cache_write
    if batch:
        if entry.batch_multiplier is None:
            return None
        total *= entry.batch_multiplier
    return round(total, 10)


def estimate_usd(request_chars: int, max_tokens: int, entry: PriceEntry) -> float:
    """Pessimistic pre-call estimate: every input character at 3.5 per token, and the
    full max_tokens of output."""
    tokens_in = request_chars / ESTIMATE_CHARS_PER_TOKEN
    return round(tokens_in / PER * entry.input + max_tokens / PER * entry.output, 10)
