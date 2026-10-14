"""Unicode-aware character classes for Python's `re`, which has no \\p{Lu}.

Bibliographies are multilingual: a reader that only knows A–Z and Latin-1
silently drops Šmid, Łukasiewicz, Čapek, Ōtsuka, Şahin or Иванов — or merges
their entries into the previous one. These classes cover every cased letter in
the Basic Multilingual Plane (Latin, Greek, Cyrillic, Armenian, Georgian, …).
"""

from __future__ import annotations

import re


def _class(predicate) -> str:
    ranges, start, previous = [], None, None
    for code in range(0x41, 0x10000):
        if 0xD800 <= code <= 0xDFFF:
            continue
        if predicate(chr(code)):
            if start is None:
                start = code
            previous = code
            continue
        if start is not None:
            ranges.append((start, previous))
            start = None
    if start is not None:
        ranges.append((start, previous))
    parts = [re.escape(chr(a)) if a == b else f"{re.escape(chr(a))}-{re.escape(chr(b))}" for a, b in ranges]
    return "[" + "".join(parts) + "]"


UPPER = _class(str.isupper)
LOWER = _class(str.islower)
LETTER = r"[^\W\d_]"
# A letter, apostrophe or hyphen: the characters of a printed surname.
NAME_CHAR = r"(?:[^\W\d_]|['’\-])"

__all__ = ["LETTER", "LOWER", "NAME_CHAR", "UPPER"]
