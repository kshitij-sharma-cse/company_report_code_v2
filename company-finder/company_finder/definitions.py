"""Document-local definitions → scoped aliases (no LLM).

``Acme Holdings, LLC ("the Sponsor")`` makes later ``the Sponsor`` an alias hit
(note ``from_definition``), but only inside the same exhibit/section after the
definition. A blank or missing section label is not an exhibit id: the alias
stays on that TextUnit and is never applied file-wide. ``Legal Name (the "Company")`` is special:

* if that legal name is the filer/registrant, hits are filer self-refs and are
  suppressed from the default mention list (they would flood every 10-K);
* if the legal name is some *other* listed company, ``the Company`` is kept
  only in that exhibit/section after the definition.

Co-occurrence (name and "the Company" on the same line without definition
syntax) is ignored. Short tokens (GS/AI/US, length ≤ 2) are never aliases.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .mention import Mention

# Gap between a matched legal name and the definition marker: punctuation and
# legal suffixes only — not arbitrary words (that would be co-occurrence).
_SUFFIX = (
    r"l\.?\s*l\.?\s*c\.?|l\.?\s*l\.?\s*p\.?|l\.?\s*p\.?|inc\.?|corp\.?|corporation|"
    r"ltd\.?|limited|plc|co\.?|company|n\.?\s*a\.?|p\.?\s*l\.?\s*c\.?|gmbh|s\.?\s*a\.?"
)
_GAP_ONLY = re.compile(rf"^(?:[\s,.;:&]|\band\b|{_SUFFIX})+$", re.I)

_PAREN = re.compile(
    r"""\(\s*
        (?:hereinafter(?:\s+referred\s+to\s+as)?|referred\s+to\s+as|collectively,?)?
        \s*,?\s*
        (?:the\s+|an?\s+)?
        ["\u201c']([^"\u201d']{1,50})["\u201d']
        \s*\)""",
    re.I | re.X,
)
_HEREIN = re.compile(
    r"""hereinafter(?:\s+referred\s+to\s+as)?\s*,?\s*
        (?:the\s+)?
        ["\u201c']([^"\u201d']{1,50})["\u201d']""",
    re.I | re.X,
)
_MEANS = re.compile(
    r"""\bmeans\s+(?:the\s+)?["\u201c']([^"\u201d']{1,50})["\u201d']""",
    re.I | re.X,
)
# Unbridged role definitions (legal name not already a list-company hit).
_ROLE_PAREN = re.compile(
    r"""([A-Z][\w.&'’\-]{1,40}(?:\s+[\w.&'’\-]{1,40}){0,8})\s*,?\s*
        (?:(?:l\.?\s*l\.?\s*c\.?|inc\.?|corp\.?|corporation|ltd\.?|limited)\.?\s*)?
        \(\s*(?:the\s+)?["\u201c']((?:the\s+)?[^"\u201d']{1,40})["\u201d']\s*\)""",
    re.X,
)

_FILER_INNER = {"company", "companies", "registrant", "issuer"}
_ROLE_INNER = {
    "sponsor", "buyer", "target", "purchaser", "seller", "acquirer", "acquiror",
    "borrower", "guarantor", "parent", "lender",
}
_SHORT_WORDS = {"gs", "ai", "us", "uk", "eu", "it", "id", "co", "lp", "sa", "na", "ag"}


@dataclass
class _Def:
    display: str
    regex: re.Pattern
    company_id: str
    canonical_name: str
    source_text: str
    scope: str                 # "section" | "document"
    scope_key: str
    unit_index: int
    after_offset: int
    self_ref: bool
    suppress: bool


def _inner(term: str) -> str:
    t = " ".join(term.split())
    t = re.sub(r"^(?:the|a|an)\s+", "", t, flags=re.I)
    return t.strip()


def _too_short(term: str) -> bool:
    inner = _inner(term)
    letters = re.sub(r"[^A-Za-z0-9]", "", inner)
    if len(letters) <= 2:
        return True
    if inner.lower() in _SHORT_WORDS:
        return True
    if re.fullmatch(r"(?:[A-Za-z]\.){1,4}", inner):
        return True
    return False


def _display(raw: str, had_the_prefix: bool) -> str:
    term = " ".join(raw.split())
    if had_the_prefix and not term.lower().startswith(("the ", "a ", "an ")):
        return f"the {term}"
    return term


def _term_regex(display: str) -> re.Pattern:
    body = r"\s+".join(re.escape(p) for p in display.split())
    return re.compile(rf"(?<![\w&]){body}(?:['\u2019]s)?(?![\w&])", re.I)


def _span(m: Mention) -> tuple[int, int] | None:
    if hasattr(m, "_abs"):
        return m._abs
    if m.char_start is None:
        return None
    return (m.char_start, m.char_end)


def _fence(unit, offset: int, unit_index: int) -> tuple[str, str]:
    """Return ``(scope, scope_key)`` for a non-filer definition.

    A real section/exhibit label is shared by later units in that exhibit.
    A blank or missing label is fenced to this TextUnit only — an empty string
    must not collapse the whole filing into one exhibit.
    """
    from .runner import _section_at

    sec = (_section_at(unit, offset) or "").strip()
    loc = unit.location or {}
    part = str(loc.get("part") or "")
    doc = str(loc.get("doc") or "")
    if sec:
        return "section", f"{doc}|{part}|{sec}"
    return "unit", f"{doc}|{part}|unit:{unit_index}"


def _labeled_scope(unit, offset: int) -> str | None:
    """Shared exhibit key, or None when the section label is blank."""
    from .runner import _section_at

    sec = (_section_at(unit, offset) or "").strip()
    if not sec:
        return None
    loc = unit.location or {}
    return f"{loc.get('doc') or ''}|{loc.get('part') or ''}|{sec}"


def _role_display(raw: str) -> str:
    inner = _inner(raw)
    if inner.lower().startswith("the "):
        return inner
    return f"the {inner}"


def _earliest_def(window: str):
    """Return (match, group_index 1-based, kind) of the earliest definition marker."""
    best = None
    for rx in (_PAREN, _HEREIN, _MEANS):
        m = rx.search(window)
        if not m:
            continue
        if best is None or m.start() < best.start():
            best = m
    return best


def _parse_one(window: str) -> tuple[str, int] | None:
    """Definition inside ``window`` (text after a company mention).

    Returns (display term, end offset within window) or None.
    """
    m = _earliest_def(window)
    if not m:
        return None
    gap = window[:m.start()]
    if gap and not _GAP_ONLY.match(gap):
        return None
    raw = next(g for g in m.groups() if g)
    prefix = window[m.start():m.start(1)]
    had_the = bool(re.search(r"\bthe\b", prefix, re.I))
    display = _display(raw, had_the)
    if _too_short(display):
        return None
    return display, m.end()


def _collect(unit_mentions, filer_ids: set[str]) -> list[_Def]:
    defs: list[_Def] = []
    for idx, (unit, mentions) in enumerate(unit_mentions):
        if unit.cells:
            continue
        for m in mentions:
            if m.match_type in ("fuzzy", "cik", "defined_term", "indirect"):
                continue
            sp = _span(m)
            if not sp:
                continue
            _a, b = sp
            window = unit.text[b:b + 180]
            parsed = _parse_one(window)
            if not parsed:
                continue
            display, rel_end = parsed
            if display.lower() == (m.matched_text or "").lower():
                continue
            inner = _inner(display).lower()
            is_filer_label = inner in _FILER_INNER
            company_is_filer = m.company_id in filer_ids
            if is_filer_label and company_is_filer:
                scope, self_ref, suppress = "document", True, True
                scope_key = _fence(unit, _a, idx)[1]
            else:
                # Non-filer "the Company" AND role nicknames: same exhibit only.
                # Blank section → this TextUnit, never the whole filing.
                scope, scope_key = _fence(unit, _a, idx)
                self_ref, suppress = False, False
            after = b + rel_end
            defs.append(_Def(
                display=display,
                regex=_term_regex(display),
                company_id=m.company_id,
                canonical_name=m.canonical_name,
                source_text=m.matched_text,
                scope=scope,
                scope_key=scope_key,
                unit_index=idx,
                after_offset=after,
                self_ref=self_ref,
                suppress=suppress,
            ))
    return defs


def _active(defs: list[_Def], unit_index: int, hit: int, scope_key: str) -> _Def | None:
    chosen = None
    for d in defs:
        if d.scope != "document" and d.scope_key != scope_key:
            continue
        if unit_index < d.unit_index:
            continue
        if unit_index == d.unit_index and hit < d.after_offset:
            continue
        if chosen is None or (d.unit_index, d.after_offset) >= (chosen.unit_index, chosen.after_offset):
            chosen = d
    return chosen


def apply_scoped_definitions(unit_mentions, doc: str, filer_ids: set[str] | None = None, *,
                             include_self_ref: bool = False
                             ) -> tuple[list[Mention], list[str]]:
    """Append scoped alias mentions onto ``unit_mentions`` and return them.

    Filer ``the Company`` self-refs are omitted unless ``include_self_ref``.
    A warning counts how many were suppressed.
    """
    from .context import snippet
    from .runner import _enrich_evidence, _place, _section_at, _text_loc

    filer_ids = set(filer_ids or ())
    defs = _collect(unit_mentions, filer_ids)
    if not defs:
        return [], []
    added: list[Mention] = []
    suppressed = 0
    for idx, (unit, mentions) in enumerate(unit_mentions):
        if unit.cells:
            continue
        taken = [sp for sp in (_span(m) for m in mentions) if sp]
        scopes = [(0, len(unit.text), {})]
        for s0, e0, cell_loc in scopes:
            occupied = list(taken)
            found = []
            for d in defs:
                for mobj in d.regex.finditer(unit.text, s0, e0):
                    a = mobj.start()
                    if unit.text[a:a + len(d.display)].lower() == d.display.lower():
                        b = a + len(d.display)
                    else:
                        b = mobj.end()
                    active = _active(defs, idx, a, _fence(unit, a, idx)[1])
                    if active is None:
                        continue
                    if (active.display.lower() != d.display.lower()
                            or active.company_id != d.company_id
                            or (active.unit_index, active.after_offset) != (d.unit_index, d.after_offset)):
                        continue
                    found.append((a, b, d))
            found.sort(key=lambda t: (t[0], t[1]))
            for a, b, hit_def in found:
                if any(a < y and x < b for x, y in occupied):
                    continue
                occupied.append((a, b))
                if hit_def.suppress and not include_self_ref:
                    suppressed += 1
                    continue
                nm = Mention(
                    company_id=hit_def.company_id,
                    canonical_name=hit_def.canonical_name,
                    matched_text=unit.text[a:b],
                    match_type="alias",
                    score=90.0,
                    char_start=a - s0,
                    char_end=b - s0,
                    alias=hit_def.display,
                    context=snippet(unit.text, a, b, 160),
                    matcher="definition",
                    note=f'from_definition; defined term for "{hit_def.source_text}"',
                )
                nm._abs = (a, b)
                nm.extras = {"from_definition": True, "definition_scope": hit_def.scope}
                if hit_def.self_ref:
                    nm.extras["self_ref"] = True
                    nm.extras["self_ref_kind"] = "filer"
                    extra_note = "filer self-ref (defined 'Company' for registrant)"
                    if extra_note not in nm.note:
                        nm.note = "; ".join(x for x in (nm.note, extra_note) if x)
                nm.section = _section_at(unit, a) if not unit.cells else unit.section
                _enrich_evidence(nm, unit.text, a, b)
                loc = {**_text_loc(unit, a), **(cell_loc or {})}
                _place(nm, loc, doc)
                mentions.append(nm)
                added.append(nm)
    warnings = []
    if suppressed:
        warnings.append(
            f"suppressed {suppressed} filer self-ref definition hit(s) "
            "(the Company/registrant); pass include_filer_self_ref_definitions to keep"
        )
    return added, warnings


def unbridged_role_unit_indexes(unit_mentions) -> list[int]:
    """Units with a role nickname definition whose legal name is NOT a list hit.

    Generic filer words (company / our / we) are not included — those would
    mark almost every 10-K page. Later nickname pages are added by
    :func:`expand_unbridged_nickname_indexes`, not here.
    """
    out = []
    for idx, (unit, mentions) in enumerate(unit_mentions):
        if unit.cells or not unit.text:
            continue
        spans = [sp for sp in (_span(m) for m in mentions) if sp]
        for m in _ROLE_PAREN.finditer(unit.text):
            name_start, name_end = m.start(1), m.end(1)
            if any(name_start < y and x < name_end for x, y in spans):
                continue
            inner = _inner(m.group(2)).lower()
            if inner not in _ROLE_INNER or _too_short(m.group(2)):
                continue
            out.append(idx)
            break
    return out


def expand_unbridged_nickname_indexes(units, def_indexes) -> list[int]:
    """Def units plus later units in the same exhibit where the nickname appears.

    Blank/missing section: the definition unit only. Does not pull every
    ``the Company`` / ``our`` / ``we`` page, and does not walk a different exhibit.
    """
    wanted: list[int] = []
    seen_idx: set[int] = set()
    for idx in def_indexes or []:
        if not isinstance(idx, int) or not (0 <= idx < len(units)) or idx in seen_idx:
            continue
        seen_idx.add(idx)
        unit = units[idx]
        wanted.append(idx)
        if getattr(unit, "cells", None) or not getattr(unit, "text", None):
            continue
        for match in _ROLE_PAREN.finditer(unit.text):
            inner = _inner(match.group(2)).lower()
            if inner not in _ROLE_INNER or _too_short(match.group(2)):
                continue
            scope = _labeled_scope(unit, match.start())
            if not scope:
                continue
            rx = _term_regex(_role_display(match.group(2)))
            for j in range(idx + 1, len(units)):
                other = units[j]
                if getattr(other, "cells", None) or not getattr(other, "text", None):
                    continue
                for hit in rx.finditer(other.text):
                    if _labeled_scope(other, hit.start()) == scope:
                        wanted.append(j)
                        break
    out: list[int] = []
    seen: set[int] = set()
    for i in wanted:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out
