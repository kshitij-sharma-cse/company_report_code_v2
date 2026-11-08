"""Collapse same-spelling company rows, and drop excluded names before matching.

Cluster key is the soft-normalized *full* name body plus a suffix **class**.
Suffixes are not stripped down to a bare stem:

* Inc / Incorporated → ``inc``
* Limited / Ltd → ``limited`` (does **not** merge with Inc)
* LLC, LP, Corp/Corporation, Co/Company → each its own class (Corp ≠ Inc)

``BlackRock Inc`` and ``BLACKROCK, INC.`` share a cluster. ``Blackrock Limited``,
``BlackRock Corp``, ``BlackRock Fund Advisors Inc``, and ``BlackRock, Inc. (US)``
do not. Dedupe never merges those into one entity.

Stem fan-out (a bare ``BlackRock`` hitting Inc and Limited together) is handled
in the matcher, using :func:`legal_form` so the suffix classes stay the same.
"""
from __future__ import annotations

import argparse
import re
import csv
import logging
import sys
from collections import Counter
from pathlib import Path

from .loader import Company, _split, load_companies, normalize_sheet_args
from .normalise import alias_tokens

log = logging.getLogger(__name__)

# Last-token legal form → class. Unknown endings stay in the body.
SUFFIX_CLASS = {
    "inc": "inc",
    "incorporated": "inc",
    "limited": "limited",
    "ltd": "limited",
    "llc": "llc",
    "lp": "lp",
    "corp": "corp",
    "corporation": "corp",
    "co": "co",
    "company": "co",
}

CSV_FIELDS = (
    "id", "canonical_name", "aliases", "ticker", "cik",
    "case_sensitive_aliases", "exchange_only_tickers", "exclude_phrases",
)


def name_tokens(name: str) -> list[str]:
    """Lower-case tokens with punctuation already dropped (same as matching)."""
    return [t.lower() for t in alias_tokens(name or "")]


def soft_name(name: str) -> str:
    return " ".join(name_tokens(name))


def cluster_key(name: str) -> tuple[str, str]:
    """Return ``(body, suffix_class)``.

    Only a recognized legal suffix in the **final** token is peeled off.
    ``(US)`` and any other qualifier stays in the body, so
    ``BlackRock, Inc. (US)`` does not join ``BlackRock Inc``.
    A suffix with an empty body (the word ``Inc`` alone) is not peeled.
    """
    toks = name_tokens(name)
    if not toks:
        return ("", "")
    suffix = SUFFIX_CLASS.get(toks[-1], "")
    if suffix and len(toks) > 1:
        return (" ".join(toks[:-1]), suffix)
    return (" ".join(toks), "")



_PAREN_RE = re.compile(r"\(([^)]*)\)")
_TRAIL_PAREN_RE = re.compile(r"\s*\(([^)]{1,40})\)")

# Parenthetical jurisdictions only. "(HUL)" is a ticker, not a jurisdiction.
_JURISDICTION = {
    "us", "usa", "uk", "eu", "hk", "uae", "prc",
    "cayman", "delaware", "bermuda", "singapore", "india", "china", "japan",
    "canada", "ireland", "luxembourg", "netherlands", "germany", "france",
    "australia", "switzerland", "hongkong", "hong kong",
}
_JURIS_ALIAS = {"usa": "us", "us": "us", "uk": "uk", "hongkong": "hong kong"}


def _norm_jurisdiction(inner: str) -> str | None:
    """``(US)`` / ``(U.S.)`` / ``(Cayman)`` → a jurisdiction key, else None."""
    raw = (inner or "").strip()
    if not raw or len(raw) > 40:
        return None
    compact = re.sub(r"[^A-Za-z]+", "", raw).lower()
    spaced = " ".join(re.sub(r"[.]", "", raw).lower().split())
    if compact not in _JURISDICTION and spaced not in _JURISDICTION:
        return None
    return _JURIS_ALIAS.get(compact, compact if compact in _JURISDICTION else spaced)


def peel_jurisdictions(name: str) -> tuple[str, str]:
    """Drop jurisdiction parentheses. Return ``(rest, jurisdiction)``."""
    found: list[str] = []

    def repl(match: re.Match) -> str:
        key = _norm_jurisdiction(match.group(1))
        if not key:
            return match.group(0)
        if key not in found:
            found.append(key)
        return " "

    return _PAREN_RE.sub(repl, name or ""), " ".join(found)


def legal_form(name: str) -> tuple[str, str, str]:
    """``(body, suffix_class, jurisdiction)`` for a declared name or a doc span.

    ``(US)`` is peeled *before* the suffix class, so ``BlackRock, Inc. (US)``
    is ``("blackrock", "inc", "us")``. :func:`cluster_key` is unchanged and
    still refuses to merge that row with ``BlackRock Inc``.
    """
    stripped, juris = peel_jurisdictions(name)
    body, suffix = cluster_key(stripped)
    return (body, suffix, juris)


def surface_form(text: str, start: int, end: int) -> tuple[str, str, str]:
    """Form of a matched span, plus a jurisdiction paren sitting just after it.

    ``BlackRock, Inc.`` followed immediately by ``(US)`` counts as the ``(US)``
    form even when the matcher span stopped on ``Inc``.
    """
    span = text[start:end]
    extra = _TRAIL_PAREN_RE.match(text[end:end + 48])
    if extra and _norm_jurisdiction(extra.group(1)):
        span = f"{span} ({extra.group(1).strip()})"
    return legal_form(span)


def company_matches_surface(company, surface: tuple[str, str, str]) -> bool:
    """True when a *declared* name (canonical or explicit alias) has this form.

    Auto-stripped cores (``BlackRock Inc`` → ``BlackRock``) are not declared
    forms, so they do not keep a hit. That is what stops suffix fan-out.
    """
    if not surface or not surface[0]:
        return False
    names = [company.canonical_name, *company.aliases, *company.case_sensitive_aliases]
    for name in names:
        if name and str(name).strip() and legal_form(str(name)) == surface:
            return True
    return False


def _unique(items) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in items:
        for part in _split(raw) if not isinstance(raw, list) else raw:
            text = str(part).strip()
            if text and text not in seen:
                seen.add(text)
                out.append(text)
    return out


def _pick_canonical(group: list[Company]) -> Company:
    """Most common exact spelling, then longest string, then earliest row."""
    counts = Counter(c.canonical_name for c in group)
    index = {id(c): i for i, c in enumerate(group)}

    def rank(c: Company):
        return (-counts[c.canonical_name], -len(c.canonical_name), index[id(c)])

    return min(group, key=rank)


def _merge_group(group: list[Company]) -> Company:
    canon = _pick_canonical(group)
    aliases: list[str] = []
    seen: set[str] = set()

    def add(text: str) -> None:
        text = (text or "").strip()
        if not text or text == canon.canonical_name or text in seen:
            return
        seen.add(text)
        aliases.append(text)

    for a in canon.aliases:
        add(a)
    for other in group:
        if other is canon:
            continue
        add(other.canonical_name)
        for a in other.aliases:
            add(a)
    tickers = _unique([c.ticker for c in group])
    ciks = [c.cik for c in group if c.cik]
    # Prefer the canonical row's CIK when it has one; do not invent a merge.
    cik = canon.cik or (ciks[0] if ciks else None)
    return Company(
        id=canon.id,
        canonical_name=canon.canonical_name,
        aliases=aliases,
        ticker="|".join(tickers) or None,
        case_sensitive_aliases=_unique(c.case_sensitive_aliases for c in group),
        exchange_only_tickers=_unique(c.exchange_only_tickers for c in group),
        cik=cik,
        exclude_phrases=_unique(c.exclude_phrases for c in group),
    )


def dedupe_companies(companies: list[Company]) -> list[Company]:
    """Collapse same body+suffix-class rows. Order follows the first row of each cluster."""
    buckets: dict[tuple[str, str], list[Company]] = {}
    order: list[tuple[str, str]] = []
    for company in companies:
        key = cluster_key(company.canonical_name)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(company)
    return [_merge_group(buckets[key]) for key in order]


def _matches_exclude(name: str, needles: list[str]) -> bool:
    norm = soft_name(name)
    if not norm:
        return False
    tokens = set(norm.split())
    for needle in needles:
        if not needle:
            continue
        if needle in tokens or needle in norm:
            return True
    return False


def exclude_companies(companies: list[Company], names: list[str]) -> list[Company]:
    """Drop a company when its canonical name **or any alias** soft-matches.

    Match is a normalized token or a substring of the space-joined normalized
    name. ``BlackRock`` therefore drops ``BlackRock Inc``, ``BLACKROCK, INC.``,
    and ``Blackrock Limited``. Call this on the **deduped** list so every
    merged spelling is checked, not just the canonical one.
    """
    needles = [soft_name(n) for n in names if n and str(n).strip()]
    needles = [n for n in needles if n]
    if not needles:
        return list(companies)
    kept: list[Company] = []
    for company in companies:
        surfaces = [company.canonical_name, *company.aliases, *company.case_sensitive_aliases]
        if any(_matches_exclude(s, needles) for s in surfaces):
            continue
        kept.append(company)
    return kept


def prepare_companies(companies: list[Company], *, dedupe: bool = False,
                      exclude: list[str] | None = None) -> list[Company]:
    """Load-time pipeline: optional dedupe, then exclude. Never a post-CSV filter."""
    out = companies
    if dedupe:
        before = len(out)
        out = dedupe_companies(out)
        log.info("dedupe: %d -> %d companies", before, len(out))
    if exclude:
        before = len(out)
        out = exclude_companies(out, exclude)
        log.info("exclude: %d -> %d companies", before, len(out))
    if not out:
        raise ValueError("no companies left after dedupe/exclude")
    return out


def read_exclude_file(path: str | Path) -> list[str]:
    names: list[str] = []
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.append(line)
    return names


def collect_excludes(names: list[str] | None, path: str | Path | None) -> list[str]:
    out: list[str] = []
    for name in names or []:
        if name and str(name).strip():
            out.append(str(name).strip())
    if path:
        out.extend(read_exclude_file(path))
    return out


def write_companies_csv(companies: list[Company], path: str | Path) -> None:
    path = Path(path)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for company in companies:
            writer.writerow({
                "id": company.id,
                "canonical_name": company.canonical_name,
                "aliases": "|".join(company.aliases),
                "ticker": company.ticker or "",
                "cik": company.cik or "",
                "case_sensitive_aliases": "|".join(company.case_sensitive_aliases),
                "exchange_only_tickers": "|".join(company.exchange_only_tickers),
                "exclude_phrases": "|".join(company.exclude_phrases),
            })


def main(argv: list[str] | None = None) -> int:
    """Offline dedupe. Does not edit the source workbook."""
    parser = argparse.ArgumentParser(
        prog="python -m company_finder.dedupe",
        description="Cluster same-spelling company rows (body + suffix class) "
                    "into one canonical name. Other spellings become aliases. "
                    "Inc and Limited stay separate. Does not merge (US) into Inc.",
    )
    parser.add_argument("-c", "--companies", required=True,
                        help="company file (.xlsx/.xlsm/.csv/.json/.txt)")
    parser.add_argument("--companies-sheet", action="append", default=None, metavar="NAME",
                        help="Excel sheet (repeat or comma-separate). Default: all usable sheets")
    parser.add_argument("-o", "--out", required=True, help="deduped CSV path")
    parser.add_argument("--exclude", action="append", default=None, metavar="NAME",
                        help="drop companies whose name or alias soft-matches (repeatable)")
    parser.add_argument("--exclude-file", metavar="PATH",
                        help="extra exclude names, one per line")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sheets = normalize_sheet_args(args.companies_sheet)
    companies = load_companies(args.companies, sheets=sheets)
    before = len(companies)
    companies = prepare_companies(
        companies,
        dedupe=True,
        exclude=collect_excludes(args.exclude, args.exclude_file),
    )
    write_companies_csv(companies, args.out)
    print(f"deduped {before} -> {len(companies)} companies -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
