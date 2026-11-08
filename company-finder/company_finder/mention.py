"""Result types shared by matchers, the runner and output writers."""
from __future__ import annotations

import re
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


def unit_key(m: Mention) -> tuple:
    """Where a mention's char offsets live: its extraction unit (and table cell).

    ``location_detail`` minus ``line`` (derived from the offset): PDF page,
    EDGAR ``doc``, HTML ``text_block`` / ``table``+``row``+``col``, sheet+cell,
    DOCX paragraph ... Falls back to ``page`` when no location was recorded.
    Without it, unpaged formats (HTML/iXBRL/TXT) would put every unit at page
    None and offset 0..n in one bucket.
    """
    loc = m.location_detail or {}
    items = tuple(sorted((str(k), str(v)) for k, v in loc.items() if k != "line"))
    return items or (("page", str(m.page)),)


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
    Key = ``(document, page, unit_key, char_start, char_end, matched_text)`` when ``char_end >
    char_start``. Offsets are local to the extract unit (PDF page, HTML text block,
    table row *cell*), so :func:`unit_key` is part of the key — without it two
    HTML table rows both starting with the name at ``0:13`` would wrongly merge.
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
            # unit (page / block / table row+cell) so local offsets in different units stay apart
            # + matched_text: one span of one unit has one text, so this only splits
            # units that share a label (Form D: two issuerPreviousName values)
            return ("span", m.document or "", m.page, unit_key(m),
                    int(m.char_start), int(m.char_end), m.matched_text or "")
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


_TABLE_LINE = re.compile(r"\S(?:\t| {2,})[$(]?\d")  # fixed-width text table: gap then a number


def _passage_scope(m: Mention, body: str) -> tuple:
    """Paragraph identity inside the document: unit (page / text block / table
    row, cells of one row together) plus, for a fixed-width text table (most
    lines have a column gap before a number), the line -- each line is a row."""
    scope = tuple(x for x in unit_key(m) if x[0] not in ("col", "cell"))
    lines = [ln for ln in (m.passage or "").split("\n") if ln.strip()]
    if len(lines) > 1 and m.line_number is not None and \
            2 * sum(1 for ln in lines if _TABLE_LINE.search(ln)) >= len(lines):
        scope += (("line", str(m.line_number)),)
    return scope


_DIGIT = re.compile(r"\d")


def _passage_base(group: list[Mention]) -> int | None:
    """Offset of ``passage`` in the hits' unit text: the one shift that puts every
    hit's ``matched_text`` at its char span in the (shared) passage, else None
    (hits in different cells of one row use cell-local offsets)."""
    first, text = group[0], group[0].passage or ""
    surf = first.matched_text or ""
    if not surf or first.char_start is None:
        return None
    pos = text.find(surf)
    while pos >= 0:
        base = first.char_start - pos
        if all(m.char_start is not None and m.char_end is not None and m.char_start >= base
               and text[m.char_start - base: m.char_end - base].lower() == (m.matched_text or "").lower()
               for m in group):
            return base
        pos = text.find(surf, pos + 1)
    return None


def _split_rows(group: list[Mention]) -> list[list[Mention]]:
    """Split one paragraph's hits into data rows: a digit (date, amount, share
    count) between two hits means they sit on separate rows of a schedule --
    any format (PDF page, <pre>, minified HTML, TXT). No digit between them =
    a repeated name in prose, kept together."""
    base = _passage_base(group) if len(group) > 1 else None
    if base is None:
        return [group]
    text = group[0].passage or ""
    hits = sorted(group, key=lambda m: (m.char_start, m.char_end))
    rows, cur = [], [hits[0]]
    for m in hits[1:]:
        end = max(x.char_end for x in cur)
        if _DIGIT.search(text[end - base: m.char_start - base] if m.char_start > end else ""):
            rows.append(cur)
            cur = []
        cur.append(m)
    rows.append(cur)
    return rows


def dedupe_passages(mentions: list[Mention]) -> list[Mention]:
    """One mention per company per paragraph (run after :func:`dedupe_output`).

    Once a company's name or any alias has matched a paragraph, further hits
    of that **same company** in that paragraph add no evidence. Key =
    ``(company_id, document, page, unit, passage)`` with whitespace normalised
    (``context`` when ``passage`` is empty); unit = :func:`_passage_scope`
    (text block / table row / page, and the line in a fixed-width text table),
    so two table rows -- even with identical text -- stay separate and an
    unpaged HTML/TXT filing is not collapsed to one row. Inside a paragraph,
    hits with a digit between them are separate data rows (:func:`_split_rows`:
    PDF schedule page, <pre>, minified HTML). Per row keeps the best score,
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
        key = (("p", m.company_id, m.document or "", str(m.page), _passage_scope(m, body), body)
               if body else ("noscope", idx))
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(m)

    out: list[Mention] = []
    for key in order:
        rows = _split_rows(buckets[key]) if key[0] == "p" else [buckets[key]]
        for group in rows:
            out.append(_collapse(group))
    return out


def _collapse(group: list[Mention]) -> Mention:
    """Keep the best hit of one row (score, then earliest); record the rest."""
    if len(group) == 1:
        return group[0]
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
    return keep
