"""Load the company master list from CSV, JSON, Excel or plain TXT."""
from __future__ import annotations

import csv
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# How many leading rows to scan for a header (title rows above the header are ok).
_EXCEL_HEADER_SCAN = 20
_EXCEL_SUFFIXES = {".xlsx", ".xlsm"}


@dataclass
class Company:
    id: str
    canonical_name: str
    # Short description used only by the LLM common-word disambiguation pass
    # (never affects matching itself). Empty means that pass skips this company.
    description: str = ""
    aliases: list[str] = field(default_factory=list)
    ticker: str | None = None            # may hold several, pipe-separated
    case_sensitive_aliases: list[str] = field(default_factory=list)
    # Tickers matched ONLY after an exchange prefix ("(NYSE: GM)"), never alone.
    exchange_only_tickers: list[str] = field(default_factory=list)
    cik: str | None = None               # SEC Central Index Key (digits)
    # Phrases that contain one of this company's names but refer to something
    # else; a match inside them is suppressed (e.g. "Shell company").
    exclude_phrases: list[str] = field(default_factory=list)

    @property
    def tickers(self) -> list[str]:
        return _split(self.ticker)


def normalise_cik(value) -> str | None:
    """'0001234567' / '1234567' / 1234567 -> '1234567' (None if not digits)."""
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    return digits.lstrip("0") or None if digits else None


def _split(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = str(value).split("|")
    return [s.strip() for s in items if s and str(s).strip()]


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "company"


def _from_record(rec: dict, idx: int) -> Company | None:
    # Normalize keys the same way as Excel headers (spaces/hyphens → _).
    rec = {_norm_header(k): v for k, v in rec.items() if k is not None}
    name = str(
        rec.get("canonical_name")
        or rec.get("name")
        or rec.get("company_name")
        or rec.get("legal_name")
        or rec.get("company")
        or rec.get("entity_name")
        or ""
    ).strip()
    if not name:
        return None
    cid = str(rec.get("id") or "").strip() or f"{idx}-{_slug(name)}"
    ticker = str(rec.get("ticker") or "").strip() or None
    return Company(
        id=cid,
        canonical_name=name,
        description=str(rec.get("description") or "").strip(),
        aliases=_split(rec.get("aliases")),
        ticker=ticker,
        case_sensitive_aliases=_split(rec.get("case_sensitive_aliases")),
        exclude_phrases=_split(rec.get("exclude_phrases")),
        exchange_only_tickers=_split(rec.get("exchange_only_tickers")),
        cik=normalise_cik(rec.get("cik")),
    )


def normalize_sheet_args(sheets) -> list[str] | None:
    """Turn ``--companies-sheet`` values into a de-duplicated name list.

    Accepts a string, a list, and comma-separated names. ``None`` / empty
    means every usable sheet.
    """
    if sheets is None:
        return None
    if isinstance(sheets, str):
        sheets = [sheets]
    out: list[str] = []
    seen: set[str] = set()
    for item in sheets:
        if item is None:
            continue
        for part in str(item).split(","):
            name = part.strip()
            if name and name not in seen:
                seen.add(name)
                out.append(name)
    return out or None


def _excel_cell(value):
    """Excel numbers often come back as float (1234567.0) — keep whole numbers as int text."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    return value


NAME_HEADER_KEYS = frozenset({
    "canonical_name", "name", "company_name", "legal_name",
    "company", "entity_name",
})


def _norm_header(value) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower()
    return re.sub(r"[\s\-]+", "_", text)


def _check_duplicate_ids(companies: list[Company], where: str) -> None:
    dupes = [cid for cid, n in Counter(c.id for c in companies).items() if n > 1]
    if dupes:
        raise ValueError(f"{where}: duplicate company ids {sorted(set(dupes))[:10]}")


def load_companies_many(paths, sheets=None) -> list[Company]:
    """Load and concatenate several company files (ids must stay unique).

    ``sheets`` applies only to Excel files (``.xlsx`` / ``.xlsm``).
    """
    if isinstance(paths, (str, Path)):
        paths = [paths]
    sheet_names = normalize_sheet_args(sheets)
    out: list[Company] = []
    for p in paths:
        out.extend(load_companies(p, sheets=sheet_names))
    dupes = [cid for cid, n in Counter(c.id for c in out).items() if n > 1]
    if dupes:
        raise ValueError(f"duplicate company ids across files: {sorted(set(dupes))[:10]}")
    return out


def load_companies(path: str | Path, sheets=None) -> list[Company]:
    """Load companies. Format is chosen by extension (.csv/.tsv/.json/.xlsx/.xlsm/.txt).

    For Excel, every sheet with a company-name header (``canonical_name`` / ``name`` / ``company_name`` / ``legal_name`` / …) is loaded
    unless ``sheets`` names specific worksheets (missing names are an error).
    """
    path = Path(path)
    ext = path.suffix.lower()
    sheet_names = normalize_sheet_args(sheets)
    companies: list[Company] = []
    if ext in _EXCEL_SUFFIXES:
        companies = _load_excel(path, sheet_names)
    elif ext in (".csv", ".tsv"):
        if sheet_names:
            log.warning("%s: --companies-sheet ignored (not an Excel company file)", path)
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh, delimiter="\t" if ext == ".tsv" else ",")
            cols = {_norm_header(c) for c in (reader.fieldnames or [])}
            if not (NAME_HEADER_KEYS & cols):
                raise ValueError(
                    f"{path}: CSV needs a company name column "
                    f"(canonical_name/name/company_name/legal_name/…); found {sorted(cols)}"
                )
            for i, rec in enumerate(reader, 1):
                c = _from_record(rec, i)
                if c:
                    companies.append(c)
    elif ext == ".json":
        if sheet_names:
            log.warning("%s: --companies-sheet ignored (not an Excel company file)", path)
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            data = data.get("companies", [])
        for i, rec in enumerate(data, 1):
            if isinstance(rec, str):
                rec = {"canonical_name": rec}
            c = _from_record(rec, i)
            if c:
                companies.append(c)
    else:  # .txt or anything else: one name per line, '#' comments
        if sheet_names:
            log.warning("%s: --companies-sheet ignored (not an Excel company file)", path)
        for i, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            companies.append(Company(id=f"{i}-{_slug(line)}", canonical_name=line))
    _check_duplicate_ids(companies, str(path))
    if not companies:
        raise ValueError(f"{path}: no companies found")
    return companies


def _companies_from_worksheet(ws, idx: int) -> tuple[list[Company], int, str | None]:
    """Read one sheet. Returns (companies, next_idx, skip_reason or None)."""
    rows = ws.iter_rows(values_only=True)
    header: list[str] | None = None
    scanned = 0
    for row in rows:
        scanned += 1
        keys = [_norm_header(c) for c in (row or ())]
        if NAME_HEADER_KEYS & set(keys):
            header = keys
            break
        if scanned >= _EXCEL_HEADER_SCAN:
            break
    if not header:
        return [], idx, "no company name header (canonical_name/name/company_name/legal_name/…)"
    out: list[Company] = []
    for row in rows:
        if row is None or not any(c is not None and str(c).strip() for c in row):
            continue
        idx += 1
        rec: dict = {}
        for i, key in enumerate(header):
            if not key or i >= len(row):
                continue
            if key not in rec:
                rec[key] = _excel_cell(row[i])
        company = _from_record(rec, idx)
        if company:
            out.append(company)
    if not out:
        return [], idx, "header present but no company rows"
    return out, idx, None


def _load_excel(path: Path, sheets: list[str] | None) -> list[Company]:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        available = list(wb.sheetnames)
        if sheets:
            missing = [name for name in sheets if name not in available]
            if missing:
                raise ValueError(
                    f"{path}: company sheet(s) not found: {missing} "
                    f"(workbook has {available})"
                )
            selected = list(sheets)
        else:
            selected = available
        companies: list[Company] = []
        skipped: list[str] = []
        idx = 0
        for name in selected:
            loaded, idx, reason = _companies_from_worksheet(wb[name], idx)
            if reason:
                skipped.append(f"{name} ({reason})")
                log.warning("%s: skipping sheet %r (%s)", path, name, reason)
                continue
            companies.extend(loaded)
        if not companies:
            detail = f"; skipped: {', '.join(skipped)}" if skipped else ""
            raise ValueError(f"{path}: no companies found{detail}")
        return companies
    finally:
        wb.close()
