"""What counts as a name, in one place.

Two passes need the same answers and had begun to hold their own copies: the policy's
second pass, which masks name-shaped words no recogniser claimed, and the sweep, which
licenses the parts of a detected person's name across the rest of the document. A
disagreement between them is a leak or an over-redaction, depending which way it falls,
so the judgements live here and both import them.
"""

from __future__ import annotations

# Titles that precede a name and are not part of it.
HONORIFICS = frozenset(
    {"mr", "mrs", "ms", "mx", "miss", "dr", "prof", "hon", "sir", "madam", "rev", "sgt", "cst"}
)

# Lower-case words that sit inside a surname: `van der Merwe`, `de Souza`, `von Hannover`.
# Joined into a name run only between two parts of it, never at either end, so a sentence
# that happens to contain "de" or "van" is not masked on that account. `nee` is the
# unaccented spelling people type for `née`.
PARTICLES = frozenset(
    {
        "van", "der", "den", "de", "del", "della", "di", "da", "das", "dos", "du", "von",
        "zu", "ter", "ten", "le", "la", "bin", "ibn", "al", "el", "née", "nee",
    }
)  # fmt: skip

# Letters that read as initials and are not a person: a run of initials standing alone is
# masked unless its letters are one of these.
COMMON_INITIALISMS = frozenset(
    {
        "uk", "us", "usa", "eu", "un", "ussr", "am", "pm", "eg", "ie", "nb", "ad", "bc",
        "phd", "llb", "llm", "ba", "ma", "bsc", "msc", "qc", "kc", "mp", "cv", "op",
    }
)  # fmt: skip

# The right single quotation mark, built from its code point because the formatter would
# otherwise write the character itself into this file, and this repository keeps to plain
# punctuation.
RSQUO = chr(0x2019)

_STRIP = ".,;:'" + RSQUO


def is_name_shaped(token: str) -> bool:
    """Whether a word could be part of somebody's name, judged on case alone.

    Two or more letters, starting with a capital. An internal capital is fine and is the
    point: `MacDonald`, `McCarthy`, `LeBlanc`, `O'Brien` and `Jean-Pierre` are all one word.
    Accented and non-Latin letters count, because `isupper` knows about them and a character
    class written in ASCII does not. An all-capitals word needs three letters, so `OK` and
    `NL` stay readable while `GAGNE` and `PENASHUE` do not.

    A caseless script carries no signal this function can read, so Chinese, Arabic and
    Inuktitut syllabics are never name-shaped. That is a stated limit, not an oversight:
    the alternative is masking every word in those scripts, which would make a Labrador
    document unreadable. Such names need a detector, not a shape.
    """
    letters = [c for c in token if c.isalpha()]
    if len(letters) < 2 or not letters[0].isupper():
        return False
    return not (len(letters) < 3 and all(c.isupper() for c in letters))


def name_parts(name: str) -> list[str]:
    """The parts of a detected name, in order, with honorifics and punctuation dropped.

    Hyphens split, so `Jean-Pierre Roche` offers `Jean`, `Pierre` and `Roche`: a document
    that later says only `Pierre` means the same person.
    """
    parts = []
    for part in name.replace("-", " ").split():
        cleaned = part.strip(_STRIP)
        if len(cleaned) >= 2 and cleaned.casefold() not in HONORIFICS:
            parts.append(cleaned)
    return parts


__all__ = ["COMMON_INITIALISMS", "HONORIFICS", "PARTICLES", "RSQUO", "is_name_shaped", "name_parts"]
