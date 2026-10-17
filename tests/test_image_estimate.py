"""The pre-call estimate counts an inline image by a fixed pessimistic allowance, not by its
base64 characters, in each vendor's request shape (1.4.2)."""

from __future__ import annotations

import base64
import json

from boundary.ledger.prices import (
    ESTIMATE_CHARS_PER_TOKEN,
    IMAGE_ESTIMATE_TOKENS,
    estimate_chars,
)

PAGE = base64.b64encode(bytes(range(256)) * 4096).decode("ascii")  # about 1.4 MB
ALLOWANCE = round(IMAGE_ESTIMATE_TOKENS * ESTIMATE_CHARS_PER_TOKEN)


def body(content: list[dict[str, object]]) -> bytes:
    return json.dumps({"messages": [{"role": "user", "content": content}]}).encode()


SHAPES: dict[str, dict[str, object]] = {
    "anthropic": {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": PAGE},
    },
    "openai": {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{PAGE}", "detail": "high"},
    },
    "google": {"inline_data": {"mime_type": "image/png", "data": PAGE}},
}


def test_each_vendors_inline_image_is_counted_at_the_allowance() -> None:
    for shape in SHAPES.values():
        raw = body([shape, {"type": "text", "text": "Read this page."}])
        assert estimate_chars(raw) == len(raw) - len(PAGE) + ALLOWANCE


def test_two_images_get_two_allowances() -> None:
    raw = body([SHAPES["anthropic"], SHAPES["anthropic"]])
    assert estimate_chars(raw) == len(raw) - 2 * len(PAGE) + 2 * ALLOWANCE


def test_text_is_counted_as_it_was() -> None:
    long_word = "x" * 5000
    raw = body([{"type": "text", "text": f"{long_word} and a short data field"}])
    assert estimate_chars(raw) == len(raw)
    short = body([{"type": "text", "text": "hi", "data": "QUJD"}])
    assert estimate_chars(short) == len(short)
