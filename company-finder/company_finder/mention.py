"""Result types shared by matchers, the runner and output writers."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Mention:
    company_id: str
    canonical_name: str
    matched_text: str
    match_type: str          # 'exact' | 'alias' | 'fuzzy' | 'ticker' | 'cik' | 'defined_term' | 'indirect'
    score: float
    char_start: int          # offsets into the ORIGINAL text of the unit
    char_end: int
    alias: str = ""          # the variant that matched
    context: str = ""        # short sentence snippet (~300 chars)
    passage: str = ""        # full blank-line paragraph containing the mention
    line_number: int | None = None  # 1-based page/unit line when newlines exist
    extras: dict = field(default_factory=dict)  # dynamic future fields (citation, metrics, ...)
    ambiguous: bool = False  # same span matched several companies
    matcher: str = ""        # which matcher produced it
    note: str = ""           # e.g. "part of longer entity name ..." / "CIK match"
    section: str | None = None   # filing section (Item 1A, Schedule of Investments, ...)
    is_filer: bool = False   # company is the filer/issuer of the document
    # filled in by the document runner
    document: str | None = None
    page: Any = None         # PDF page / slide number / sheet name
    location: str | None = None
    location_detail: dict = field(default_factory=dict)
    tok_span: tuple[int, int] | None = field(default=None, repr=False)

    @property
    def page_number(self) -> Any:
        """Clearer alias of ``page`` (PDF page / slide / sheet)."""
        return self.page

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("tok_span", None)
        d["page_number"] = d.get("page")
        return d


@dataclass
class Passage:
    """A span of text with no detected mention (future extension hook).

    Stable shape (Tester / Researcher compatible)::

        {
          "passage_id": str,   # stable id within the unit, e.g. "p0"
          "text": str,         # passage text (== unit_text[char_start:char_end])
          "char_start": int,   # 0-based offset into the MatchContext / unit text
          "char_end": int,     # exclusive end offset
        }

    ``start`` / ``end`` are retained as read-only aliases of ``char_start`` /
    ``char_end`` for older call sites.
    """
    char_start: int
    char_end: int
    text: str
    passage_id: str = ""

    @property
    def start(self) -> int:
        return self.char_start

    @property
    def end(self) -> int:
        return self.char_end

    def to_dict(self) -> dict[str, Any]:
        return {
            "passage_id": self.passage_id,
            "text": self.text,
            "char_start": self.char_start,
            "char_end": self.char_end,
        }


def dedupe_identical(mentions: list[Mention]) -> list[Mention]:
    """Drop repeated hits of the same company on the same span.

    Keeps the higher score. Different companies on one span stay (true ambiguity).
    """
    best: dict[tuple, Mention] = {}
    order: list[tuple] = []
    for mention in mentions:
        key = (mention.company_id, mention.char_start, mention.char_end,
               mention.match_type, mention.matched_text)
        prev = best.get(key)
        if prev is None:
            best[key] = mention
            order.append(key)
        elif mention.score > prev.score:
            best[key] = mention
    return [best[key] for key in order]


def dedupe_output(mentions: list[Mention]) -> list[Mention]:
    """Collapse multi-company hits on the same document char span (OUT-level).

    Applied **after** mentions are built (Phase-1 matcher, and the same stage
    after Phase-2 LLM rows). Default **on** in the CLI (``--no-dedupe-output``
    to keep every ambiguous row). Does **not** merge Master Inc↔Limited.

    Grouping
    --------
    Key = ``(document, page, char_start, char_end)`` when ``char_end > char_start``.
    ``page`` is the extract unit (PDF page / slide / sheet) because offsets are
    unit-local — without it, page 1 and page 2 at ``0:9`` would wrongly merge.
    Never group by ``matched_text`` alone; never whole-file collapse.

    Rows **without** a usable char span (future LLM rows) each keep their own
    identity — they are **not** merged onto Phase-1 hits by company name.

    Selection within a span group
    -----------------------------
    Keep **one** mention. Prefer the company whose canonical
    :func:`legal_form` matches :func:`legal_form` of ``matched_text``
    (bare ``BlackRock`` → unsuffixed ``BLACKROCK``; ``Inc`` surface → Inc;
    ``Limited`` → Limited; ``(US)`` → US). On ties, higher ``score``, then
    ``company_id``. Same ``company_id`` duplicates on one span also collapse.

    Multiple ``BLACKROCK`` rows on different pages / sentences stay separate.
    """
    from .dedupe import legal_form

    if not mentions:
        return []

    def has_span(m: Mention) -> bool:
        return (
            m.char_start is not None
            and m.char_end is not None
            and m.char_end > m.char_start
        )

    def group_key(m: Mention, idx: int) -> tuple:
        if has_span(m):
            # page = extract unit so local offsets on different pages stay apart
            return ("span", m.document or "", m.page, int(m.char_start), int(m.char_end))
        # No span: never merge by name / matched_text (LLM-safe)
        return ("noscope", idx)

    def rank(m: Mention) -> tuple:
        surface = legal_form(m.matched_text or "")
        canon = legal_form(m.canonical_name or "")
        form_miss = 0 if (surface[0] and canon == surface) else 1
        return (form_miss, -float(m.score or 0), str(m.company_id or ""))

    buckets: dict[tuple, list[Mention]] = {}
    order: list[tuple] = []
    for idx, mention in enumerate(mentions):
        key = group_key(mention, idx)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(mention)

    out: list[Mention] = []
    for key in order:
        group = buckets[key]
        if len(group) == 1:
            out.append(group[0])
        else:
            out.append(min(group, key=rank))
    return out


def _norm_text(text: str | None) -> str:
    return " ".join(str(text or "").split())


def mention_hits(m) -> int:
    """Raw hits a (possibly passage-collapsed) mention stands for."""
    extras = (m.get("extras") if isinstance(m, dict) else getattr(m, "extras", None)) or {}
    if isinstance(extras, str):  # CSV round-trip
        try:
            import json
            extras = json.loads(extras) or {}
        except ValueError:
            extras = {}
    try:
        return max(1, int(extras.get("hits_in_passage", 1)))
    except (TypeError, ValueError, AttributeError):
        return 1


def dedupe_passages(mentions: list[Mention]) -> list[Mention]:
    """One mention per company per paragraph (run after :func:`dedupe_output`).

    Once a company's name or any alias has matched a paragraph, further hits
    of that **same company** in that paragraph (same document + page) add no
    evidence. Key = ``(company_id, document, page, passage)`` with whitespace
    normalised (``context`` when ``passage`` is empty). Keeps the best score,
    then earliest ``char_start``. Collapsed surfaces go to
    ``extras["also_matched"]`` and the raw hit count to
    ``extras["hits_in_passage"]`` so ``mention_count`` stays honest.

    Different companies in one paragraph stay separate rows. Rows with no
    passage/context text are never merged.
    """
    buckets: dict[tuple, list[Mention]] = {}
    order: list[tuple] = []
    for idx, m in enumerate(mentions):
        body = _norm_text(m.passage) or _norm_text(m.context)
        key = (("p", m.company_id, m.document or "", str(m.page), body)
               if body else ("noscope", idx))
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(m)

    out: list[Mention] = []
    for key in order:
        group = buckets[key]
        if len(group) == 1:
            out.append(group[0])
            continue
        keep = min(group, key=lambda m: (-float(m.score or 0), m.char_start or 0))
        others: list[str] = []
        for m in group:
            surface = m.matched_text or ""
            if surface and surface != keep.matched_text and surface not in others:
                others.append(surface)
        extras = dict(keep.extras or {})
        extras["hits_in_passage"] = sum(mention_hits(m) for m in group)
        if others:
            extras["also_matched"] = others
        keep.extras = extras
        out.append(keep)
    return out

