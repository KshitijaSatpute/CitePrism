"""Text normalization with reversible character locations; no fuzzy quote acceptance."""

import hashlib
import json
import re
import unicodedata


def digest_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_json(value):
    return digest_text(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False))


def canonical_id(value):
    """Unify numeric citation labels without erasing meaningful author/year text."""
    text = str(value if value is not None else "").strip()
    numeric = re.fullmatch(r"(?:\[\s*(\d+)\s*\]|\(\s*(\d+)\s*\)|(\d+)\s*[.)]?)", text)
    if numeric:
        digits = next(group for group in numeric.groups() if group is not None)
        return digits.lstrip("0") or "0"
    return re.sub(r"[\[\]\s]", "", text).lower()


def normalized_locations(text):
    """Collapse whitespace only, retaining original offsets for every output character."""
    characters, locations = [], []
    for index, character in enumerate(text):
        if character.isspace():
            if characters and characters[-1] != " ":
                characters.append(" ")
                locations.append(index)
        else:
            characters.append(character)
            locations.append(index)
    return "".join(characters), locations


def locate_quote(quote, source):
    normalized, positions = normalized_locations(source)
    needle = normalized_locations(quote)[0].strip()
    if len(needle) < 20 or needle.casefold() in {"no abstract available", "no source available"}:
        return None
    start = normalized.find(needle)
    if start < 0:
        return None
    end = start + len(needle)
    return {"char_start": positions[start], "char_end": positions[end - 1] + 1,
            "text": source[positions[start]:positions[end - 1] + 1],
            "source_text_sha256": digest_text(source)}


def locate_manuscript_quote(quote, source):
    """Locate a claim, allowing a PDF line break after a retained word hyphen.

    For example, real-\ntime and real-time have the same letters/punctuation.
    Never remove a hyphen, change wording, or apply this rule to source quotes.
    Returned text and offsets always refer to the original manuscript context.
    """
    exact = locate_quote(quote, source)
    if exact:
        return exact
    skipped = set()
    for match in re.finditer(r'(?<=\w)-([^\S\r\n]*\r?\n[^\S\r\n]*)(?=\w)', source):
        skipped.update(range(match.start(1), match.end(1)))
    if not skipped:
        return None
    positions = [index for index in range(len(source)) if index not in skipped]
    joined = ''.join(source[index] for index in positions)
    located = locate_quote(quote, joined)
    if not located:
        return None
    start, end = positions[located['char_start']], positions[located['char_end'] - 1] + 1
    return {'char_start': start, 'char_end': end, 'text': source[start:end],
            'source_text_sha256': digest_text(source)}


def bibliographic_text(text):
    """Comparison only. Never use this normalization to validate source quotes."""
    return "".join(c for c in unicodedata.normalize("NFKD", text or "").casefold()
                   if c.isalnum())


def marker_ids(marker):
    if not re.fullmatch(r"\[\s*\d[\d\s,;–—-]*\]", marker):
        return [canonical_id(marker)]
    result = []
    for item in re.split(r"[,;]", marker.strip("[] ")):
        bounds = re.split(r"\s*[-–—]\s*", item.strip())
        if len(bounds) == 1:
            result.append(str(int(bounds[0])))
        elif len(bounds) == 2 and 0 < int(bounds[0]) <= int(bounds[1]) <= int(bounds[0]) + 200:
            result.extend(str(i) for i in range(int(bounds[0]), int(bounds[1]) + 1))
    return list(dict.fromkeys(result))
