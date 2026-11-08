"""Text normalisation with exact offset mapping back to the original text.

Pipeline (applied identically to documents and to company aliases):

1. Character level: Unicode NFKD, accents/non-spacing marks dropped
   ("Nestlé" -> "Nestle"), ligatures expanded ("ﬁ" -> "fi"), curly quotes /
   dashes / exotic spaces unified, zero-width characters and soft hyphens
   removed.  Every output character remembers the [start, end) span of the
   original character(s) it came from.
2. Token level: the text is split into word tokens (runs of letters/digits,
   including Indic combining marks).  "&" is its own token and is read as
   "and", so "L&T" == "L & T" == "L and T".  All other punctuation (dots,
   commas, apostrophes, hyphens, brackets) is a separator, which makes
   "J.P. Morgan" -> "j p morgan", "Infosys's" -> "infosys s".
3. The *key text* is the lower-cased tokens joined by single spaces.  Aho-
   Corasick runs over the key text; a match is valid only if it starts and ends
   on token boundaries (= word-boundary matching).  Token spans map back to
   exact original offsets via the character map.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

# Characters mapped to a replacement (or deleted when mapped to "").
_PUNCT_MAP = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'",
    "\u00b4": "'", "`": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2033": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",
    "\u2015": "-", "\u2212": "-", "\ufe63": "-", "\uff0d": "-",
    "\uff06": "&", "\ufe60": "&",
    "\u00ad": "",   # soft hyphen
    "\u200b": "", "\u200c": "", "\u200d": "", "\u2060": "", "\ufeff": "",
    "\u00a0": " ", "\u2007": " ", "\u202f": " ", "\u3000": " ",
}

# Letters/digits (\w minus underscore) plus combining marks, including Indic
# spacing marks (category Mc) which \w does not cover.
_TOKEN_RE = re.compile(r"(?:[^\W_]|[\u0300-\u036f\u0900-\u0dff])+|&")


@lru_cache(maxsize=65536)
def _map_char(ch: str) -> str:
    rep = _PUNCT_MAP.get(ch)
    if rep is not None:
        return rep
    if ch.isspace():
        return "\n" if ch in "\n\r\u2028\u2029\x0b\x0c\x85" else " "
    out = []
    for c in unicodedata.normalize("NFKD", ch):
        if unicodedata.category(c) == "Mn":
            out.append("\x00")  # marker: attach to previous char
        else:
            out.append(_PUNCT_MAP.get(c, c))
    return "".join(out)


def normalise_chars(text: str) -> tuple[str, list[int] | None, list[int] | None]:
    """Return (normalised_text, starts, ends).

    ``starts[i]``/``ends[i]`` give the original [start, end) span of normalised
    character ``i``.  For pure-ASCII input the mapping is the identity and
    ``(text, None, None)`` is returned (fast path).
    """
    if text.isascii():
        return text, None, None
    out: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    for i, ch in enumerate(text):
        if ch < "\x80":
            out.append(ch)
            starts.append(i)
            ends.append(i + 1)
            continue
        for c in _map_char(ch):
            if c == "\x00":  # dropped combining mark: extend previous char
                if ends:
                    ends[-1] = i + 1
                continue
            out.append(c)
            starts.append(i)
            ends.append(i + 1)
    return "".join(out), starts, ends


@dataclass(slots=True)
class Token:
    norm_start: int
    norm_end: int
    cased: str   # token as written ("&" becomes "and")
    key: str     # lower-cased form used for matching


def tokenize(norm: str) -> list[Token]:
    toks = []
    for m in _TOKEN_RE.finditer(norm):
        s = m.group()
        if s == "&":
            toks.append(Token(m.start(), m.end(), "and", "and"))
        else:
            toks.append(Token(m.start(), m.end(), s, s.lower()))
    return toks


class NormalizedText:
    """A document text prepared for matching, with offset mapping."""

    __slots__ = ("original", "norm", "starts", "ends", "tokens", "key",
                 "key_starts", "tok_at_start", "tok_at_end")

    def __init__(self, text: str):
        self.original = text
        self.norm, self.starts, self.ends = normalise_chars(text)
        self.tokens = tokenize(self.norm)
        parts = []
        key_starts = []
        pos = 0
        for t in self.tokens:
            key_starts.append(pos)
            parts.append(t.key)
            pos += len(t.key) + 1
        self.key = " ".join(parts)
        self.key_starts = key_starts
        self.tok_at_start = {ks: i for i, ks in enumerate(key_starts)}
        self.tok_at_end = {ks + len(t.key): i for i, (ks, t) in enumerate(zip(key_starts, self.tokens))}

    # -------------------------------------------------------------- offsets
    def orig_span(self, first_tok: int, last_tok: int) -> tuple[int, int]:
        """Original [start, end) for tokens first_tok..last_tok inclusive."""
        ns = self.tokens[first_tok].norm_start
        ne = self.tokens[last_tok].norm_end
        if self.starts is None:
            return ns, ne
        return self.starts[ns], self.ends[ne - 1]

    def gap(self, left_tok: int, right_tok: int) -> str:
        """Normalised text between two adjacent tokens."""
        return self.norm[self.tokens[left_tok].norm_end:self.tokens[right_tok].norm_start]

    def preceding(self, tok: int) -> str:
        """Normalised text between the previous token (or start) and ``tok``."""
        start = self.tokens[tok - 1].norm_end if tok > 0 else 0
        return self.norm[start:self.tokens[tok].norm_start]


def alias_tokens(s: str) -> list[str]:
    """Cased tokens of an alias / company name, normalised like documents."""
    norm, _, _ = normalise_chars(s)
    return [t.cased for t in tokenize(norm)]


def key_of(tokens: list[str] | tuple[str, ...]) -> str:
    return " ".join(t.lower() for t in tokens)
