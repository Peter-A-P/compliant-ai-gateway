"""The preserve line: one system-prompt sentence asking a model to copy placeholders exactly.

It lives in the library, not the proxy, so the measurement that tests it
(`boundary.redact.mutation`, arm `proxy`) and the proxy that sends it
(`boundary.server.redaction`) use the same string, and the harness needs no server extra.

0.14.0 measured an earlier line that gave `<PERSON_1>` and `<EMAIL_2>` as examples, and found
Llama 3.3 70B copying the example into its answers. This one gives no example.
"""

from __future__ import annotations

PRESERVE_LINE = (
    "The text contains placeholders in angle brackets, each standing in for a personal "
    "detail that was removed before the text reached you. Copy every placeholder you use "
    "exactly, character for character, angle brackets included. Never invent, renumber, "
    "merge or replace one, and do not guess what it stands for."
)

__all__ = ["PRESERVE_LINE"]
