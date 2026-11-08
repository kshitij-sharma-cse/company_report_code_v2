"""Run a CompanyMatcher over files/folders, optionally in parallel."""
from __future__ import annotations

import bisect
import re
import logging
import os
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .context import line_number_at, paragraph, snippet
from .extract import SUPPORTED_EXT, ExtractOptions, TextUnit, extract
from .match import CompanyMatcher
from .mention import Mention

log = logging.getLogger(__name__)


@dataclass
class DocumentResult:
    path: str
    doc_type: str = ""
    units: int = 0
    info: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    seconds: float = 0.0
    mentions: list[Mention] = field(default_factory=list)
    text_chars: int = 0
    filer: dict | None = None               # filer/issuer facts (SGML header, iXBRL dei, Form D)
    filer_company_ids: list[str] = field(default_factory=list)
    dropped: list[Mention] = field(default_factory=list)  # removed by a matcher rule (name-tail)


def discover(inputs: list[str | Path], recursive: bool = True) -> list[Path]:
    """Expand files and folders into a sorted, de-duplicated file list."""
    out: list[Path] = []
    seen = set()
    for inp in inputs:
        p = Path(inp)
        if p.is_dir():
            it = p.rglob("*") if recursive else p.glob("*")
            files = sorted(f for f in it if f.is_file() and f.suffix.lower() in SUPPORTED_EXT
                           and not f.name.startswith(("~$", ".")))
        elif p.is_file():
            files = [p]
        else:
            raise FileNotFoundError(f"input not found: {p}")
        for f in files:
            r = f.resolve()
            if r not in seen:
                seen.add(r)
                out.append(f)
    return out


def location_label(loc: dict) -> str:
    base = _location_label(loc)
    if "doc" in loc:
        return f"{loc['doc']}" + (f" {base}" if base else "")
    return base


def _location_label(loc: dict) -> str:
    if "form_d_field" in loc:
        return f"Form D {loc['form_d_field']}"
    if "text_block" in loc and "line" not in loc:
        return f"block {loc['text_block']}"
    if "page" in loc:
        return f"p.{loc['page']}"
    if "slide" in loc:
        return f"slide {loc['slide']}" + (" notes" if loc.get("part") == "notes" else "")
    if "sheet" in loc:
        return f"{loc['sheet']}!{loc.get('cell', '')}".rstrip("!")
    if "table" in loc:
        return f"table {loc['table']} r{loc.get('row')}c{loc.get('col')}"
    if "paragraph" in loc:
        return f"para {loc['paragraph']}"
    if "part" in loc:
        return f"{loc['part']} (section {loc.get('section', 1)})"
    if "cell" in loc:
        return str(loc["cell"])
    if "line" in loc:
        return f"line {loc['line']}"
    return ""


def _section_at(unit: TextUnit, offset: int) -> str | None:
    sec = unit.section
    for off, label in unit.sections or ():
        if off <= offset:
            sec = label
        else:
            break
    return sec


_SIG_LINE = re.compile(r"/s/[^\n]{0,60}$")


def _signature_person(unit: TextUnit, m: Mention) -> None:
    """'By: /s/ Erik Halvorsen' - a one-word alias on a signature line is most
    likely a person's surname: keep it, but with a low score and a note."""
    if m.match_type in ("ticker", "cik") or len(m.matched_text.split()) > 1 or not hasattr(m, "_abs"):
        return
    a = m._abs[0]
    line_start = unit.text.rfind("\n", 0, a) + 1
    if _SIG_LINE.search(unit.text[line_start:a]):
        m.score = min(m.score, 50.0)
        m.note = "; ".join(x for x in (m.note, "on a /s/ signature line (person's name?)") if x)


def match_unit(matcher: CompanyMatcher, unit: TextUnit, doc: str, *,
               joined_lines: bool = False, dropped: list | None = None) -> list[Mention]:
    """Mentions in one unit. ``joined_lines``: the extractor joined visual lines
    (PDF). Matches removed by a matcher rule are appended to ``dropped``."""
    out = _match_unit(matcher, unit, doc, joined_lines, dropped)
    for m in out:
        _signature_person(unit, m)
    for m in out:
        if m.section is None:
            m.section = _section_at(unit, m.char_start) if not unit.cells else unit.section
    if unit.cik:
        _apply_cik(matcher, unit, doc, out)
    return out


def _apply_cik(matcher: CompanyMatcher, unit: TextUnit, doc: str, out: list[Mention]) -> None:
    """Structured CIK (Form D issuer): exact key.  Confirm name mentions of the
    same company, or add a CIK-only mention when the name did not match."""
    from .loader import normalise_cik

    cik = normalise_cik(unit.cik)
    idxs = getattr(matcher, "cik_index", {}).get(cik or "", [])
    for idx in idxs:
        c = matcher.companies[idx]
        hits = [m for m in out if m.company_id == c.id]
        if hits:
            for m in hits:
                m.note = "; ".join(x for x in (m.note, f"CIK match ({unit.cik})") if x)
                m.score = max(m.score, 100.0)
            continue
        m = Mention(company_id=c.id, canonical_name=c.canonical_name, matched_text=unit.text,
                    match_type="cik", score=100.0, char_start=0, char_end=len(unit.text),
                    alias=f"CIK {c.cik}", context=unit.text[:200], matcher="cik",
                    note=f"CIK match ({unit.cik}); name in filing differs", section=unit.section)
        _enrich_evidence(m, unit.text, 0, len(unit.text))
        _place(m, dict(unit.location), doc)
        out.append(m)
    # a name hit whose company has a different CIK on file is suspicious
    for m in out:
        c = next((x for x in matcher.companies if x.id == m.company_id), None)
        if c is not None and c.cik and cik and c.cik != cik and m.match_type != "cik":
            m.note = "; ".join(x for x in (m.note, f"CIK mismatch: filing {unit.cik}, list {c.cik}") if x)
            m.score = min(m.score, 70.0)


def _match_unit(matcher: CompanyMatcher, unit: TextUnit, doc: str, joined_lines: bool = False,
                dropped: list | None = None) -> list[Mention]:
    width = matcher.config.context_max_chars
    out: list[Mention] = []
    if unit.cells:
        # match each cell separately (no matches across cell borders); context = row
        for s, e, cell_loc in unit.cells:
            ms, ctx = matcher.find_with_context(unit.text[s:e], with_context=False,
                                                joined_lines=joined_lines)
            for m in ms + ctx.dropped:
                a, b = s + m.char_start, s + m.char_end
                m.context = snippet(unit.text, a, b, width)
                m._abs = (a, b)
                _enrich_evidence(m, unit.text, a, b)
                loc = {**unit.location, **cell_loc}
                _place(m, loc, doc)
            out.extend(ms)
            if dropped is not None:
                dropped.extend(ctx.dropped)
        return out
    mentions, ctx = matcher.find_with_context(unit.text, joined_lines=joined_lines)
    for m in mentions + ctx.dropped:
        m._abs = (m.char_start, m.char_end)
        _enrich_evidence(m, unit.text, m.char_start, m.char_end)
        _place(m, _text_loc(unit, m.char_start), doc)
    out.extend(mentions)
    if dropped is not None:
        dropped.extend(ctx.dropped)
    return out


def _text_loc(unit: TextUnit, offset: int) -> dict:
    loc = dict(unit.location)
    if unit.line_numbers:
        loc["line"] = unit.text.count("\n", 0, offset) + 1
    return loc


# ------------------------------------------------------------ defined terms
_DEF_RE = re.compile(
    r"""^[\s,]*(?:\(|\[)\s*(?:together\s+with\s+[^()]{0,80}?,\s*|collectively,?\s*|
    hereinafter(?:\s+referred\s+to\s+as)?,?\s*|referred\s+to\s+as\s+)?(?:the\s+|an?\s+)?
    ["\u201c\u201d']([A-Z][\w.&\- ]{1,40}?)["\u201c\u201d']\s*(?:\)|\])""", re.X)
_GENERIC_TERMS = {"company", "companies", "borrower", "issuer", "seller", "sellers", "buyer",
                  "purchaser", "target", "parent", "merger sub", "holdings", "registrant",
                  "fund", "adviser", "advisor", "partnership", "acquisition", "merger",
                  "transaction", "agreement", "business", "subsidiary", "portfolio company",
                  "sponsor", "lender", "lenders", "guarantor", "we", "us", "our"}


def _defined_terms(unit_mentions) -> dict[str, "Mention"]:
    """``Brightwater Clinical Research, LLC ("Brightwater")`` -> {"Brightwater": mention}.
    The short term must be built from words of the matched name, so generic
    defined terms ("the Company", "the Seller") are never picked up."""
    terms: dict[str, Mention] = {}
    for unit, ms in unit_mentions:
        for m in ms:
            if m.match_type in ("fuzzy", "cik", "defined_term") or not hasattr(m, "_abs"):
                continue
            a, b = m._abs
            mm = _DEF_RE.match(unit.text[b: b + 160])
            if not mm:
                continue
            term = " ".join(mm.group(1).split())
            if term.lower() in _GENERIC_TERMS or term.lower() == m.matched_text.lower():
                continue
            name_words = {w.lower() for w in re.findall(r"\w+", m.matched_text + " " + m.canonical_name)}
            if all(w.lower() in name_words for w in re.findall(r"\w+", term)):
                terms.setdefault(term, m)
    return terms


def _apply_defined_terms(unit_mentions, doc: str) -> list[Mention]:
    terms = _defined_terms(unit_mentions)
    if not terms:
        return []
    added: list[Mention] = []
    pats = {t: re.compile(r"(?<![\w&\"\u201c'])" + re.escape(t) + r"(?:'s|\u2019s)?(?![\w&])")
            for t in terms}
    for unit, ms in unit_mentions:
        taken = [m._abs for m in ms if hasattr(m, "_abs")]
        scopes = unit.cells or [(0, len(unit.text), {})]
        for s0, e0, cell_loc in scopes:
            for term, src in terms.items():
                for hit in pats[term].finditer(unit.text, s0, e0):
                    a, b = hit.start(), hit.start() + len(term)
                    if any(a < y and x < b for x, y in taken):
                        continue
                    # "Blue Ridge Mountains": followed by another capitalised word
                    # -> a different proper noun, not the defined term
                    if re.match(r"[ \t]+[A-Z][a-z]", unit.text[hit.end(): hit.end() + 3]):
                        continue
                    taken.append((a, b))
                    nm = Mention(company_id=src.company_id, canonical_name=src.canonical_name,
                                 matched_text=term, match_type="defined_term", score=90.0,
                                 char_start=a - s0, char_end=b - s0, alias=term,
                                 context=snippet(unit.text, a, b, 160), matcher="defined_term",
                                 note=f'defined term for "{src.matched_text}"')
                    nm._abs = (a, b)
                    nm.section = _section_at(unit, a) if not unit.cells else unit.section
                    _enrich_evidence(nm, unit.text, a, b)
                    loc = {**_text_loc(unit, a), **(cell_loc or {})}
                    _place(nm, loc, doc)
                    ms.append(nm)
                    added.append(nm)
    return added



def _enrich_evidence(m: Mention, text: str, abs_start: int, abs_end: int) -> None:
    """Attach page-local line_number and full-paragraph passage (never invent lines)."""
    m.passage = paragraph(text, abs_start, abs_end)
    m.line_number = line_number_at(text, abs_start)


def _place(m: Mention, loc: dict, doc: str) -> None:
    m.document = doc
    m.location_detail = loc
    m.location = location_label(loc)
    m.page = loc.get("page", loc.get("slide", loc.get("sheet")))



_LLM_SELF_REF = re.compile(
    r"^(?:the\s+)?(?:company|companies|registrant|issuer|borrower|we|us|our|ours)$",
    re.I,
)


def _annotate_llm_self_refs(mentions: list[Mention]) -> None:
    """Mark indirect hits that look like filer self-refs (pronouns / 'the Company')."""
    for m in mentions:
        if m.match_type != "indirect":
            continue
        matched = (m.matched_text or "").strip()
        if not matched or not _LLM_SELF_REF.match(matched):
            continue
        note = "filer self-ref (pronoun/defined 'Company')"
        if m.is_filer:
            note = "filer self-ref (is_filer + pronoun/defined 'Company')"
        if note not in (m.note or ""):
            m.note = "; ".join(x for x in (m.note, note) if x)
        m.extras = dict(m.extras or {})
        m.extras.setdefault("self_ref", True)
        if m.is_filer:
            m.extras.setdefault("self_ref_kind", "filer")


def process_file(matcher: CompanyMatcher, path: str | Path, opts: ExtractOptions,
                 min_score: float = 0.0, dump_text_dir: str | None = None) -> DocumentResult:
    t0 = time.perf_counter()
    res = DocumentResult(str(path))
    try:
        ex = extract(path, opts)
        res.doc_type, res.info, res.warnings, res.units = ex.doc_type, ex.info, ex.warnings, len(ex.units)
        res.text_chars = sum(len(u.text) for u in ex.units)
        if dump_text_dir:
            _dump(ex, dump_text_dir)
        unit_mentions = []
        joined = ex.doc_type == "pdf"  # PDF blocks: visual lines joined with a space
        for unit in ex.units:
            ms = match_unit(matcher, unit, str(path), joined_lines=joined, dropped=res.dropped)
            unit_mentions.append((unit, ms))
            res.mentions.extend(m for m in ms if m.score >= min_score)
        if matcher.config.defined_terms:
            res.mentions.extend(m for m in _apply_defined_terms(unit_mentions, str(path))
                                if m.score >= min_score)
        filer = ex.info.get("filer")
        filer_ids: list[str] = []
        if filer:
            res.filer = filer
            filer_ids = filer_company_ids(matcher, filer)
            res.filer_company_ids = filer_ids
        if getattr(matcher.config, "definition_aliases", True):
            from .definitions import apply_scoped_definitions
            added, def_warnings = apply_scoped_definitions(
                unit_mentions, str(path), set(filer_ids),
                include_self_ref=getattr(
                    matcher.config, "include_filer_self_ref_definitions", False),
            )
            res.mentions.extend(m for m in added if m.score >= min_score)
            res.warnings.extend(def_warnings)
        for m in res.mentions:
            if m.company_id in filer_ids:
                m.is_filer = True
        # Flag LLM filer self-refs (our / the Company / we / us) so they are not
        # mistaken for private third-party discovery. Phase-1 definitions already
        # mark registrant "the Company" before this runs.
        _annotate_llm_self_refs(res.mentions)
    except Exception as exc:  # keep going with other files
        log.debug("failed on %s", path, exc_info=True)
        res.error = f"{type(exc).__name__}: {exc}"
    res.seconds = time.perf_counter() - t0
    return res


def filer_company_ids(matcher: CompanyMatcher, filer: dict) -> list[str]:
    """Which listed companies are the document's own filer/issuer: by CIK
    (exact), else by matching the filer name / former names as a whole."""
    from .loader import normalise_cik

    ids: list[str] = []
    cik = normalise_cik(filer.get("cik"))
    for idx in getattr(matcher, "cik_index", {}).get(cik or "", []):
        ids.append(matcher.companies[idx].id)
    for name in [filer.get("name")] + list(filer.get("former_names") or []):
        if not name:
            continue
        for m in matcher.find(name, with_context=False):
            # the filer name must be (almost) entirely covered by the alias
            covered = (m.char_end - m.char_start) / max(1, len(name.strip()))
            if m.match_type != "fuzzy" and covered >= 0.6 and m.company_id not in ids:
                ids.append(m.company_id)
    return ids


def _dump(ex, out_dir: str) -> None:
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    name = Path(ex.path).name + ".txt"
    with open(d / name, "w", encoding="utf-8") as fh:
        for u in ex.units:
            sec = f" [{u.section}]" if u.section else ""
            fh.write(f"===== {location_label(u.location) or 'text'}{sec} =====\n{u.text}\n\n")


def load_prior_hit_pages(path: str | Path) -> tuple[dict[str, set], list[str]]:
    """Hit pages from a previous Phase-1 JSON or CSV report.

    Returns ``(pages_by_document_key, input_paths)``. Document keys include the
    raw path and, when the file still exists, its resolved path.
    """
    import csv
    import json

    p = Path(path)
    pages: dict[str, set] = {}
    inputs: list[str] = []

    def _add(doc: str, page) -> None:
        if not doc:
            return
        keys = [doc]
        if Path(doc).exists():
            keys.append(str(Path(doc).resolve()))
        for key in keys:
            pages.setdefault(key, set()).add(page)

    if p.suffix.lower() == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
        for d in data.get("documents") or []:
            if d.get("path"):
                inputs.append(d["path"])
        for m in data.get("mentions") or []:
            _add(m.get("document") or "", m.get("page", m.get("page_number")))
    else:
        with p.open(encoding="utf-8", newline="") as fh:
            for m in csv.DictReader(fh):
                doc = m.get("document") or ""
                if doc and doc not in inputs:
                    inputs.append(doc)
                # Readable OUT.csv stores pages as "1,4,20". Detail CSV has page.
                raw_pages = m.get("pages")
                if raw_pages:
                    for part in str(raw_pages).split(","):
                        part = part.strip()
                        if part:
                            _add(doc, part)
                    continue
                page = m.get("page") or m.get("page_number") or None
                if page == "":
                    page = None
                _add(doc, page)
    return pages, inputs


def run_llm_pass(matcher: CompanyMatcher, results: list[DocumentResult], opts: ExtractOptions, *,
                 scope: str = "hits_and_tail", tail_pages: int = 2,
                 prior_pages: dict | None = None, min_score: float = 0.0) -> None:
    """Second pass: OpenRouter only on the scoped pages. Phase-1 mentions stay.

    Re-extracts locally (cheap) so spans line up; does not resend the whole
    filing unless ``scope="full"``. Failures become warnings, not lost Phase-1.
    """
    from .definitions import apply_scoped_definitions, unbridged_role_unit_indexes
    from .extract import extract
    from .llm import resolve_document_references

    prior_pages = prior_pages or {}
    for res in results:
        if res.error:
            continue
        try:
            ex = extract(res.path, opts)
        except Exception as exc:
            res.warnings.append(f"OpenRouter skipped (extract failed): {exc}")
            continue
        unit_mentions = []
        for unit in ex.units:
            unit_mentions.append((unit, match_unit(matcher, unit, res.path,
                                                   joined_lines=ex.doc_type == "pdf")))
        if matcher.config.defined_terms:
            _apply_defined_terms(unit_mentions, res.path)
        filer_ids = set(res.filer_company_ids or [])
        if getattr(matcher.config, "definition_aliases", True):
            apply_scoped_definitions(
                unit_mentions, res.path, filer_ids,
                include_self_ref=getattr(
                    matcher.config, "include_filer_self_ref_definitions", False),
            )
        ambiguous = unbridged_role_unit_indexes(unit_mentions)
        extra: set = set()
        raw = res.path
        resolved = str(Path(res.path).resolve()) if Path(res.path).exists() else res.path
        extra |= set(prior_pages.get(raw) or ())
        extra |= set(prior_pages.get(resolved) or ())
        try:
            llm_warnings: list[str] = []
            llm_mentions = resolve_document_references(
                matcher, ex.units, unit_mentions, res.path,
                model=matcher.config.llm_model,
                chunk_chars=matcher.config.llm_chunk_chars,
                overlap=matcher.config.llm_chunk_overlap,
                min_confidence=matcher.config.llm_min_confidence,
                max_candidates=matcher.config.llm_max_candidates,
                timeout=matcher.config.llm_timeout_seconds,
                warnings=llm_warnings,
                scope=scope,
                tail_pages=tail_pages,
                extra_hit_pages=extra,
                ambiguous_indexes=ambiguous,
                warn_pages=getattr(matcher.config, "llm_warn_pages", 25),
            )
        except Exception as exc:
            res.warnings.append(f"OpenRouter reference resolution failed: {exc}")
            continue
        seen = {(m.company_id, m.matched_text, m.char_start, m.char_end, m.page, m.section)
                for m in res.mentions}
        fresh = []
        for m in llm_mentions:
            if m.score < min_score:
                continue
            key = (m.company_id, m.matched_text, m.char_start, m.char_end, m.page, m.section)
            if key in seen:
                continue
            if m.company_id in filer_ids:
                m.is_filer = True
            fresh.append(m)
            seen.add(key)
        _annotate_llm_self_refs(fresh)
        res.mentions.extend(fresh)
        res.warnings.extend(f"OpenRouter {warning}" for warning in llm_warnings)


# ------------------------------------------------------------ parallelism
_W: dict = {}


def _init_worker(matcher: CompanyMatcher, opts: ExtractOptions, min_score: float, dump):
    _W.update(matcher=matcher, opts=opts, min_score=min_score, dump=dump)


def _work(path: str) -> DocumentResult:
    return process_file(_W["matcher"], path, _W["opts"], _W["min_score"], _W["dump"])


def run(matcher: CompanyMatcher, files: list[Path], opts: ExtractOptions | None = None, *,
        workers: int | None = None, min_score: float = 0.0,
        dump_text_dir: str | None = None) -> list[DocumentResult]:
    """Process files; results are returned in input order."""
    opts = opts or ExtractOptions()
    if workers is None:
        workers = min(len(files), os.cpu_count() or 1, 8)
    if workers <= 1 or len(files) <= 1:
        return [process_file(matcher, f, opts, min_score, dump_text_dir) for f in files]
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker,
                             initargs=(matcher, opts, min_score, dump_text_dir)) as ex:
        return list(ex.map(_work, [str(f) for f in files], chunksize=1))
