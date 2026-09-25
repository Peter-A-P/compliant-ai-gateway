"""Redaction measured end to end through the proxy (0.17): what reaches the wire.

`boundary redact eval` measures the policy on a page. This measures the proxy: every page of
the generated corpus is sent to `boundary serve` over HTTP as a `personal` request, with no
header, exactly as a client would send it, and the upstream is an in-process mock that parses
the JSON it receives and echoes the user message back. So two things can be counted that a
policy-level measurement cannot see:

- **Leaks on the wire**: a planted personal value that survives, whole or in part, anywhere
  in the messages the upstream received. Checked on the parsed body rather than its bytes,
  so an accented value hidden by JSON escaping is still found. A value survives if it is
  there whole, or if any run of three or more of its digits is, so `(709) <ID_LIKE_1>`
  counts as a leak of the area code; except a run that the page's own non-personal text
  also contains, which proves nothing. The first version of this measurement used the
  identifier set's test unmodified and reported 10 leaks, every one of them the "2024" of a
  file number `ATIPP-2024-...` matching "the 2024 budget" elsewhere on the same page, with
  the file number itself masked.
- **The round trip**: whether the answer the client receives, once rehydrated, is the page it
  sent, character for character, streamed and not.

Rules only, no model and no network, so it runs in CI. Every page goes to the local provider,
which the policy allows for personal data either way; the proxy redacts it regardless,
because `personal` names a `redacted_as`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from boundary.config import BoundaryConfig
from boundary.enforce import load_policy
from boundary.redact.corpus import build
from boundary.redact.evaluate import Rate
from boundary.server.app import create_app
from boundary.server.teams import Team, TeamsConfig, hash_key
from boundary.transport import Transport

MODEL = "local/llama3.2:3b"
KEY = "bnd_redaction-eval-key"
MODES = ("plain", "stream")


@dataclass
class ModeRow:
    mode: str
    requests: int = 0
    refused: int = 0
    planted: int = 0
    leaked: int = 0
    round_trips: int = 0

    @property
    def leak_rate(self) -> Rate:
        return Rate(self.leaked, self.planted)

    @property
    def round_trip(self) -> Rate:
        return Rate(self.round_trips, self.requests - self.refused)


@dataclass
class ProxyRedactionResults:
    boundary_version: str
    ran_utc: str
    pages: int
    seed: int
    rows: list[ModeRow]

    def table(self) -> str:
        lines = [
            f"boundary {self.boundary_version}, redaction through the proxy: {self.pages} "
            f"generated pages, seed {self.seed}, each sent as a personal request over HTTP",
            "",
            f"{'mode':<8}{'requests':>10}{'refused':>9}{'planted':>9}  "
            f"{'leaked on the wire':<28}{'came back as sent':<28}",
        ]
        for r in self.rows:
            lines.append(
                f"{r.mode:<8}{r.requests:>10}{r.refused:>9}{r.planted:>9}  "
                f"{r.leak_rate!s:<28}{r.round_trip!s:<28}"
            )
        lines += [
            "",
            "The upstream is a mock that echoes what it received, so 'came back as sent' is the "
            "whole round trip: redaction, the wire, rehydration.",
        ]
        return "\n".join(lines)

    def readme_row(self) -> str:
        """Each mode on its own. The two modes send the same values, so adding them would
        count every value twice and give an interval narrower than the evidence."""
        plain = next(r for r in self.rows if r.mode == "plain")
        stream = next(r for r in self.rows if r.mode == "stream")
        return (
            f"| {plain.leaked} of {plain.planted} plain, {plain.leak_rate}; {stream.leaked} of "
            f"{stream.planted} streamed, {stream.leak_rate} | {plain.round_trip} | "
            f"{stream.round_trip} | {plain.refused + stream.refused} of "
            f"{plain.requests + stream.requests} |"
        )


def survives(value: str, wire: str, background: str) -> bool:
    """Whether any of `value` reached `wire`: the whole of it, or a run of three or more of
    its digits that `background` (the page with every personal value cut out) does not
    already contain."""
    if value in wire:
        return True
    return any(
        run in wire and run not in background
        for run in re.findall(r"\d{3,}", value.replace(" ", ""))
    )


def _sent_user_text(body: dict[str, Any]) -> str:
    return "\n".join(
        str(m.get("content", "")) for m in body.get("messages", []) if m.get("role") == "user"
    )


class _EchoUpstream:
    """Parses each request, keeps every message it was sent, and echoes the user message."""

    def __init__(self) -> None:
        self.received: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.received.append("\n".join(str(m.get("content", "")) for m in body.get("messages", [])))
        text = _sent_user_text(body)
        if body.get("stream"):
            events = [
                b"data: "
                + json.dumps(
                    {
                        "id": "e",
                        "model": "llama3.2:3b",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": text[i : i + 5]},
                                "finish_reason": None,
                            }
                        ],
                    }
                ).encode()
                + b"\n\n"
                for i in range(0, len(text), 5)
            ]
            events.append(
                b'data: {"id":"e","choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}'
                b"\n\n"
            )
            events.append(b"data: [DONE]\n\n")
            return httpx.Response(
                200, content=b"".join(events), headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(
            200,
            json={
                "id": "e",
                "object": "chat.completion",
                "model": "llama3.2:3b",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )


def _streamed_text(raw: str) -> str:
    out = []
    for line in raw.split("\n\n"):
        if line.startswith("data: {"):
            for c in json.loads(line[6:]).get("choices", []):
                out.append(c.get("delta", {}).get("content") or "")
    return "".join(out)


def run(
    config: BoundaryConfig, policy_path: Path, *, pages: int = 200, seed: int = 20260920
) -> ProxyRedactionResults:
    from boundary import __version__

    corpus = build(pages=pages, seed=seed)
    by_page: dict[int, list[str]] = {}
    background: dict[int, str] = {}
    for i, page in enumerate(corpus.pages):
        # Labels number pages from 1, as the analyzer does. The first version of this matched
        # them against pages numbered from 0, so every value was looked for on the wrong page;
        # the check below makes that mistake fail rather than measure.
        labels = sorted(
            (lab for lab in corpus.personal if lab.page == i + 1), key=lambda x: x.start
        )
        for lab in labels:
            if page[lab.start : lab.end] != lab.text:
                raise ValueError(f"label {lab.text!r} does not select its own text on page {i + 1}")
        by_page[i] = [lab.text for lab in labels]
        parts, last = [], 0
        for lab in labels:
            parts.append(page[last : lab.start])
            last = lab.end
        parts.append(page[last:])
        background[i] = "\n".join(parts)
    teams = TeamsConfig(
        version=1,
        gateway_monthly_usd=1000.0,
        teams={
            "redaction-eval": Team(
                key_sha256=[hash_key(KEY)], monthly_usd=1000.0, requests_per_minute=10**9
            )
        },
    )
    rows = {m: ModeRow(m) for m in MODES}

    async def drive(work: Path) -> None:
        upstream = _EchoUpstream()
        app = create_app(
            config,
            teams,
            ledger_path=work / "proxy.sqlite",
            env="redaction-eval",
            policy=load_policy(policy_path),
            transport=Transport(
                config.defaults.timeouts,
                async_client=httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler)),
            ),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://redaction-eval"
        ) as client:
            for i, page in enumerate(corpus.pages):
                values = by_page.get(i, [])
                for mode in MODES:
                    row = rows[mode]
                    row.requests += 1
                    before = len(upstream.received)
                    r = await client.post(
                        "/v1/chat/completions",
                        json={
                            "model": MODEL,
                            "messages": [{"role": "user", "content": page}],
                            "max_tokens": 1000,
                            "stream": mode == "stream",
                        },
                        headers={"authorization": f"Bearer {KEY}"},
                    )
                    if r.status_code == 422:
                        row.refused += 1
                        continue
                    r.raise_for_status()
                    wire = "\n".join(upstream.received[before:])
                    row.planted += len(values)
                    row.leaked += sum(1 for v in values if survives(v, wire, background[i]))
                    got = (
                        _streamed_text(r.text)
                        if mode == "stream"
                        else r.json()["choices"][0]["message"]["content"]
                    )
                    row.round_trips += got == page
        await app.state.boundary.close()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        asyncio.run(drive(Path(tmp)))
    return ProxyRedactionResults(
        boundary_version=__version__,
        ran_utc=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        pages=pages,
        seed=seed,
        rows=list(rows.values()),
    )


README_START = "<!-- redact-proxy:start -->"
README_END = "<!-- redact-proxy:end -->"


def write_readme(readme: Path, row: str) -> None:
    text = readme.read_text(encoding="utf-8")
    start = text.index(README_START)
    end = text.index(README_END)
    readme.write_text(
        text[: start + len(README_START)] + "\n" + row + "\n" + text[end:], encoding="utf-8"
    )


__all__ = ["ModeRow", "ProxyRedactionResults", "run", "write_readme"]
