"""Nothing key-shaped is committed. PLAN.md section 8, "Secrets leak".

Scans every tracked text file in the repository, not only fixtures, because a key pasted
into a config or a doc is the same leak.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Vendor key prefixes and generic long secrets. Deliberately broad; a false positive costs
# a minute, a real key in git history costs a key rotation and a lesson.
KEY_SHAPES = [
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]

TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".yaml",
    ".yml",
    ".toml",
    ".json",
    ".jsonl",
    ".txt",
    ".cfg",
    ".ini",
    ".sql",
}


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [REPO / line for line in out.splitlines() if line]


def test_no_key_shaped_strings_in_tracked_files() -> None:
    hits: list[str] = []
    for path in _tracked_files():
        if path.suffix.lower() not in TEXT_SUFFIXES or not path.is_file():
            continue
        if path.name == "test_no_secrets.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for pat in KEY_SHAPES:
            for m in pat.finditer(text):
                hits.append(f"{path.relative_to(REPO)}: {m.group(0)[:12]}...")
    assert not hits, "key-shaped strings found:\n" + "\n".join(hits)
