"""Matcher interface - the extension point for later phases."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..context import split_sentences
from ..loader import Company
from ..mention import Mention, Passage
from ..normalise import NormalizedText


@dataclass
class MatchContext:
    text: str                      # original text of one unit (page, cell, ...)
    ntext: NormalizedText
    mentions: list[Mention] = field(default_factory=list)
    covered: bytearray = field(default_factory=bytearray)  # 1 per claimed token
    # True when the extractor joined visual lines with a space (PDF blocks, OCR):
    # a heading may then run into a name with no line break left to see.
    joined_lines: bool = False
    # matches a matcher removed by rule (Mention with extras["dropped_by"]); kept
    # so the run can report and write them instead of losing them silently
    dropped: list[Mention] = field(default_factory=list)

    def __post_init__(self):
        if not self.covered:
            self.covered = bytearray(len(self.ntext.tokens))

    def claim(self, first_tok: int, last_tok: int) -> bool:
        """Mark tokens as used; False if any was already claimed."""
        if any(self.covered[first_tok:last_tok + 1]):
            return False
        self.covered[first_tok:last_tok + 1] = b"\x01" * (last_tok - first_tok + 1)
        return True

    def make_mention(self, company: Company, first_tok: int, last_tok: int, *,
                     match_type: str, score: float, alias: str, matcher: str,
                     ambiguous: bool = False) -> Mention:
        s, e = self.ntext.orig_span(first_tok, last_tok)
        return Mention(company_id=company.id, canonical_name=company.canonical_name,
                       matched_text=self.text[s:e], match_type=match_type,
                       score=score, char_start=s, char_end=e, alias=alias,
                       ambiguous=ambiguous, matcher=matcher,
                       tok_span=(first_tok, last_tok))

    def unmatched_passages(self) -> list[Passage]:
        """Sentences that contain no mention yet.

        Future extension hook (not a live Phase-2 matcher): a later redesign can
        consume only these passages for indirect references
        ("the Mumbai-based conglomerate").

        Each :class:`~company_finder.mention.Passage` has the stable shape
        ``{passage_id, text, char_start, char_end}`` (offsets into ``self.text``).
        Sentence-level only (no ±1 neighbour window).
        """
        spans = sorted((m.char_start, m.char_end) for m in self.mentions)
        out = []
        idx = 0
        for s, e in split_sentences(self.text):
            if not any(ms < e and me > s for ms, me in spans):
                out.append(Passage(
                    char_start=s, char_end=e, text=self.text[s:e],
                    passage_id=f"p{idx}",
                ))
                idx += 1
        return out


class BaseMatcher(ABC):
    """Implement ``find``; set ``name``.  Must be picklable for multiprocessing."""

    name: str = "base"

    @abstractmethod
    def find(self, ctx: MatchContext) -> list[Mention]:
        """Return new mentions (and ``ctx.claim`` the tokens they cover)."""
