#!/usr/bin/env python3
"""Save a run's findings and its intermediate output, the way the team shares them (Kalshi_findings.zip), plus one Excel book.

    export_findings.py <co> --run <run dir>            # run dir holds dumps/ and company_report_code/
Writes <run dir>/findings/:
    <Name>_findings.xlsx      README sheet, then the findings sheets, then one sheet per intermediate step
    <Name>_findings/          the same tables as CSV, with the report PDF and HTML
    <Name>_findings.zip       that folder zipped
Findings (same files as the team's zip): holdings, nport_holdings, facts, formd_offerings, relationships_all, dol_401k (when
the run has Form 5500 data), requirements. Intermediate: filings found (step 0), N-PORT rows (0), Form D input (3),
company-finder mentions kept and dropped (1), rule-read table rows (7), recall list (7).
A table the run did not produce is written with its header only and marked "not produced" in README, so an empty sheet
always means "nothing found or not built", never a missing file.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

csv.field_size_limit(10**8)
NUM = re.compile(r"^-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?$")
NPORT_COLS = ["as_of_date", "fund_house", "holder", "security", "unit", "shares", "fair_value_usd", "price_per_share",
              "pct_net_assets", "fair_value_level", "evidence", "sec_url"]


def read(p: Path) -> tuple[list, list]:
    if not p.exists():
        return [], []
    with open(p, newline="", encoding="utf-8-sig") as fh:
        r = csv.DictReader(fh)
        return list(r.fieldnames or []), list(r)


def cell(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    s = str(v)
    if NUM.match(s) and not (len(s) > 1 and s.startswith("0") and not s.startswith("0.")) and len(s) < 16:   # keep CIKs, accessions, zero-padded ids as text
        return float(s) if any(c in s for c in ".eE") else int(s)
    return s[:32000]                                   # Excel cell limit


def write_xlsx(path: Path, title: str, sheets: list, notes: list | None = None) -> None:
    """sheets: [(sheet name, step, description, source, cols, rows, produced)]"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    ws = wb.active
    ws.title = "README"
    bold, head = Font(bold=True), PatternFill("solid", fgColor="DDE6F0")
    ws.append([title])
    ws["A1"].font = Font(bold=True, size=14)
    for n in notes or []:
        ws.append([n])
    ws.append([])
    ws.append(["Sheet", "Step", "Rows", "What it is", "Source"])
    hr = ws.max_row
    for c in ws[hr]:
        c.font, c.fill = bold, head
    for name, step, desc, src, cols, rows, produced in sheets:
        ws.append([name, step, len(rows) if produced else "not produced", desc, src])
    for col, w in zip("ABCDE", (24, 10, 13, 90, 70)):
        ws.column_dimensions[col].width = w
    for r in ws.iter_rows(min_row=hr + 1):
        r[3].alignment = Alignment(wrap_text=True, vertical="top")
    for name, step, desc, src, cols, rows, produced in sheets:
        s = wb.create_sheet(name[:31])
        s.append(cols or ["(no columns: not produced by this run)"])
        for c in s[1]:
            c.font, c.fill = bold, head
        for r in rows:
            s.append([cell(r.get(k)) for k in cols])
        if cols:
            s.freeze_panes = "A2"
            s.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{max(1, len(rows) + 1)}"
            for i, k in enumerate(cols, 1):
                width = max([len(str(k))] + [len(str(r.get(k) or "")) for r in rows[:200]])
                s.column_dimensions[get_column_letter(i)].width = max(8, min(60, width + 2))
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("company")
    ap.add_argument("--run", required=True, help="run dir: holds dumps/ and company_report_code/")
    ap.add_argument("--name", default="", help="file name stem (default: config name)")
    a = ap.parse_args()
    co, R = a.company, Path(a.run).expanduser().resolve()
    CR = R / "company_report_code" / "coref_experiment" / "company_report"
    cfg = json.loads((CR / "model_config" / f"{co}.json").read_text(encoding="utf-8"))
    name = re.sub(r"\W+", "_", a.name or cfg.get("name") or co).strip("_")
    out, dumps = CR / "out" / co, R / "dumps"
    finder = next(iter(sorted((CR.parent / "Targeted_pdf").glob(f"*/out/{co}_*"))), None)
    ev = out / "evidence"
    nport_out = out / "nport_holdings.csv"

    def t(sheet, step, desc, p: Path, default_cols=()):
        cols, rows = read(p)
        return (sheet, step, desc, str(p), cols or list(default_cols), rows, p.exists())

    find = [
        t("holdings", "6 report", "One row per real position: holder, security, shares, cost, value, dates. Numbers come from the "
          "rule-read table rows (evidence_tier A N-PORT / B stated / C review), never from the AI.", out / "holdings.csv"),
        t("nport_holdings", "0 N-PORT", "Positions funds reported on Form N-PORT (team database first, SEC XML for the rest).",
          nport_out if nport_out.exists() else dumps / f"{co}_nport" / "nport_holdings.csv", NPORT_COLS),
        t("facts", "4 AI facts", "Dated facts read by the AI from the filings' text (people, events, descriptions); each with its quote.", out / "facts.csv"),
        t("formd_offerings", "3 Form D", "The company's own Form D offerings: one row per offering, numbers from its latest notice; "
          "first_filed = date of the original notice, notices = every notice of the offering.", out / "formd_offerings.csv"),
        t("formd_notices_all", "3 Form D", "Every Form D notice the company filed, amendments included (latest_of_offering marks the one "
          "whose numbers formd_offerings uses).", out / "formd_notices_all.csv"),
        t("relationships_all", "4 AI relationships", "Who is linked to the company and how (investor, lender, acquirer ...), with the quote "
          "and the direction check.", out / "relationships_all.csv"),
        t("dol_401k", "3 Form 5500", "401(k) plan filings (Form 5500). Needs the Form 5500 data, not in the handover.", out / "dol_401k.csv"),
        t("requirements", "6 report", "Report values that need a source this run did not have.", out / "requirements.csv"),
    ]
    mid = [
        t("0 filings found", "0 download", "Every filing SEC full-text search found for the name and aliases.", dumps / f"{co}_manifest.csv"),
        t("0 nport raw rows", "0 N-PORT", "Raw N-PORT rows before the report's grouping.", dumps / f"{co}_nport" / "nport_holdings.csv"),
        t("1 mentions kept", "1 finder", "Mentions company-finder kept (the AI check said: this is the company).",
          finder / f"{co}.csv" if finder else Path("-")),
        t("1 mentions dropped", "1 finder", "Mentions company-finder dropped as look-alikes, with the reason.",
          finder / f"{co}_dropped.csv" if finder else Path("-")),
        t("3 formd input", "3 input tables", "Form D table built for the report.", CR / "inputs" / co / "data" / "FORMD_offerings.csv"),
        t("7 evidence rows", "7 evidence", "Every table row naming the company, read by rule, with unit, date and tier; "
          "'not a position' rows say why (no amount, look-alike filing ...).", ev / "evidence_rows.csv"),
        t("7 recall list", "7 evidence", "Numbers on lines naming the company that are not in the evidence rows (to read by hand).",
          ev / "recall_list.csv"),
    ]
    F = R / "findings"
    folder = F / f"{name}_findings"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    for sheet, step, desc, src, cols, rows, produced in find + mid:
        fn = re.sub(r"\W+", "_", sheet).strip("_") + ".csv"
        with open(folder / (fn if sheet in [f[0] for f in find] else "intermediate_" + fn), "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    for p in (out / f"{co}_financials_year_by_year.pdf", out / f"{co}_financials_year_by_year.html"):
        if p.exists():
            shutil.copy2(p, folder / p.name)
    xlsx = F / f"{name}_findings.xlsx"
    write_xlsx(xlsx, f"{cfg.get('company', {}).get('legal_name') or cfg.get('name') or co}: pipeline findings", find + mid,
               [f"CIK {cfg.get('company', {}).get('cik', '')}. Run folder {R}. Exported {dt.date.today()}.",
                "Made by the fixed pipeline (company_report_code_v2). Numbers in holdings come from rule-read table rows checked against N-PORT; "
                "the AI only reads text (facts, relationships, the company check in step 1)."])
    shutil.copy2(xlsx, folder / xlsx.name)
    z = F / f"{name}_findings.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(folder.rglob("*")):
            zf.write(p, Path(folder.name) / p.relative_to(folder))
    print(json.dumps({"xlsx": str(xlsx), "zip": str(z),
                      "sheets": {s[0]: (len(s[5]) if s[6] else "not produced") for s in find + mid}}, indent=1))


if __name__ == "__main__":
    sys.exit(main())
