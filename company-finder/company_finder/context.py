"""Sentence / paragraph boundaries and context snippets (abbreviation-aware)."""
from __future__ import annotations

import re

_ABBREV = {
    "mr", "mrs", "ms", "dr", "st", "co", "ltd", "inc", "corp", "no", "nos",
    "vs", "rs", "re", "jr", "sr", "pvt", "bros", "dept", "est", "fig", "approx",
    "etc", "eg", "ie", "viz", "cf", "al", "govt", "intl", "mfg", "ref", "sec",
    "art", "vol", "pp", "yr", "yrs", "mn", "bn", "cr", "mln", "bln", "avg",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct",
    "nov", "dec", "q1", "q2", "q3", "q4", "fy", "u.s", "u.k", "plc", "llc",
}
_BOUND_RE = re.compile(r"[.!?]+[\"'\u2019\u201d)\]]*\s+|\n[ \t]*\n\s*")
_PARA_BOUND_RE = re.compile(r"\n[ \t]*\n\s*")
_WORD_BEFORE_RE = re.compile(r"([\w.]+)$")
_WS_RE = re.compile(r"\s+")


def _is_boundary(text: str, m: re.Match) -> bool:
    g = m.group()
    if g.startswith("\n"):
        return True
    p = m.start()
    if text[p] == ".":
        w = _WORD_BEFORE_RE.search(text[max(0, p - 25):p])
        if w:
            word = w.group(1).lower().strip(".")
            if len(word) == 1 and word.isalpha():
                return False  # initial, e.g. "J.P. Morgan"
            if word in _ABBREV:
                return False
        nxt = text[m.end():m.end() + 1]
        if nxt and nxt.islower():
            return False
    return True


def sentence_bounds(text: str, start: int, end: int, max_chars: int = 300) -> tuple[int, int]:
    lo = max(0, start - max_chars)
    hi = min(len(text), end + max_chars)
    s = lo
    for m in _BOUND_RE.finditer(text, lo, start):
        if m.end() <= start and _is_boundary(text, m):
            s = m.end()
    e = hi
    for m in _BOUND_RE.finditer(text, end, hi):
        if _is_boundary(text, m):
            e = m.start() + len(m.group().rstrip())
            break
    return s, e


def snippet(text: str, start: int, end: int, max_chars: int = 300) -> str:
    """The sentence containing [start, end), trimmed to ~max_chars."""
    s, e = sentence_bounds(text, start, end, max_chars)
    cut_left = s > 0 and s == max(0, start - max_chars)
    cut_right = e < len(text) and e == min(len(text), end + max_chars)
    if e - s > max_chars:
        half = max(20, (max_chars - (end - start)) // 2)
        if start - half > s:
            s, cut_left = start - half, True
        if end + half < e:
            e, cut_right = end + half, True
    out = _WS_RE.sub(" ", text[s:e]).strip()
    return ("\u2026" if cut_left else "") + out + ("\u2026" if cut_right else "")


def paragraph_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """Blank-line paragraph containing [start, end).

    Boundaries are runs of blank lines (``\\n`` + optional whitespace + ``\\n``).
    If there are no blank-line breaks, the whole unit is one paragraph.
    """
    start = max(0, min(start, len(text)))
    end = max(start, min(end, len(text)))
    s = 0
    for m in _PARA_BOUND_RE.finditer(text, 0, start):
        if m.end() <= start:
            s = m.end()
    e = len(text)
    for m in _PARA_BOUND_RE.finditer(text, end, len(text)):
        e = m.start()
        break
    return s, e


def paragraph(text: str, start: int, end: int) -> str:
    """Full paragraph containing [start, end), stripped of outer whitespace."""
    s, e = paragraph_bounds(text, start, end)
    return text[s:e].strip()


def line_number_at(text: str, offset: int) -> int | None:
    """1-based line within ``text`` that contains ``offset``.

    Returns ``None`` when the unit has no newlines — never invents fake lines.
    """
    if "\n" not in text:
        return None
    offset = max(0, min(offset, len(text)))
    return text.count("\n", 0, offset) + 1


def split_sentences(text: str) -> list[tuple[int, int]]:
    """Split into (start, end) sentence spans; whitespace-only spans dropped."""
    spans = []
    pos = 0
    for m in _BOUND_RE.finditer(text):
        if _is_boundary(text, m):
            e = m.start() + len(m.group().rstrip())
            if text[pos:e].strip():
                spans.append((pos, e))
            pos = m.end()
    if text[pos:].strip():
        spans.append((pos, len(text.rstrip())))
    return spans
