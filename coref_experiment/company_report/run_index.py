#!/usr/bin/env python3
"""One index of every output file of a company's run: per filing the string-match file, its meta, the LLM facts and
relationship files; plus all string matches in one CSV. Paths only; no value is changed."""
import csv, glob, json, os, sys
from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
csv.field_size_limit(10**9)
co, dest = sys.argv[1], Path(sys.argv[2])
here = Path(__file__).resolve().parent
data = here.parent / "coref_verifier" / "data"
out = here / "out" / co
dest.mkdir(parents=True, exist_ok=True)
F = lambda p: str(Path(p).resolve()) if os.path.exists(p) else "missing"
rows, allm = [], dest / f"{co}_all_string_matches.csv"
with open(allm, "w", newline="", encoding="utf-8") as fh:
    w = None
    for mf in sorted(glob.glob(str(data / "companies" / f"{co}*__*_mentions.meta.json"))):
        m = json.load(open(mf, encoding="utf-8"))
        stem = os.path.splitext(m["document"])[0]
        mc = mf.replace(".meta.json", ".csv")
        n = 0
        for r in csv.DictReader(open(mc, encoding="utf-8")):
            if w is None:
                w = csv.writer(fh); keys = list(r); w.writerow(["form", "file_date", "filer", "accession"] + keys)
            w.writerow([m["form"], m["file_date"], m["filer"], m["accession"]] + [r[k] for k in keys]); n += 1
        rows.append([m["form"], m["file_date"], m["filer"], m["accession"], n, m.get("sec_url", ""), F(data / "pdfs" / m["document"]), F(mc), F(mf),
                     F(out / "facts" / f"{stem}.json"), F(out / "relationships" / f"{stem}_relationship_items.csv"), F(out / "relationships" / f"{stem}_relationships.csv"),
                     F(out / "relationships" / f"{stem}_relationships_review.csv"), F(out / "relationships" / f"{stem}_relationships_run.json")])
wb = Workbook(); s = wb.active; s.title = "Per filing"
H = ["Form", "Filed", "Filer", "Accession", "String matches", "SEC link", "1 Filing text", "2 String match (mentions)", "2 String match meta", "3 LLM facts + holdings", "4 Relationship items", "4 Relationships", "4 Relationships review", "4 Relationship run log"]
s.append(H); [s.append(r) for r in rows]
t = wb.create_sheet("Combined outputs")
t.append(["Stage", "File", "What it is"])
sm = []
for f in (co, co + "_ncsr"):
    d = Path("/home/amit/projectx_test/out") / f
    for x, y in (("_mentions.csv", "kept matches, used by the run"), (".csv", "companies found per filing"), ("_passages.csv", "paragraphs around each match"), ("_tables.csv", "table rows around each match"), ("_summary.csv", "count per filing"), ("_dropped.csv", "matches the model dropped as false")):
        sm.append(("1 String match run (" + f + ")", d / (f + x), y))
    sm.append(("1 String match run (" + f + ")", d / "run.log", "run log"))
for a, b, c in sm + [("2 String match", allm, "Every name match in every filing, one CSV"), ("3 LLM", out / "holdings.csv", "Positions from all filings merged"), ("3 LLM", out / "facts.csv", "Other facts merged"),
                ("4 LLM", out / "relationships_all.csv", "Relationships merged"), ("Form D", out / "formd_offerings.csv", "Form D offerings"), ("N-PORT", out / "nport_holdings.csv", "N-PORT positions"),
                ("Model", out / "model", "Financials and credit model tables (folder)"), ("Model inputs", here / "inputs" / co / "data", "Tables built from raw records (folder)"),
                ("Checks", out / "accuracy_check.csv", "Old accuracy check"), ("Checks", out / "requirements.csv", "Placeholders list"), ("Report", out / f"{co}_financials_year_by_year.pdf", "Final report PDF"),
                ("Report", out / f"{co}_financials_year_by_year.html", "Final report HTML"), ("Evidence (no LLM)", out / "evidence", "Rule-based evidence, 7 Nov (folder)"), ("Coverage", out / "exhaustiveness", "EDGAR coverage (folder)")]:
    t.append([a, F(b), c])
for ws in (s, t):
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor="1F3A5F")
    ws.freeze_panes = "A2"
for col, wd in zip("ABCDEFGHIJKLMN", [9, 11, 32, 22, 9, 20, 40, 40, 30, 40, 30, 30, 30, 30]):
    s.column_dimensions[col].width = wd
t.column_dimensions["A"].width = 18; t.column_dimensions["B"].width = 100; t.column_dimensions["C"].width = 45
wb.save(dest / f"{co}_run_outputs_index.xlsx")
miss = {H[i]: sum(r[i] == "missing" for r in rows) for i in range(6, 14)}
print(len(rows), "filings;", sum(r[4] for r in rows), "string matches; missing:", {k: v for k, v in miss.items() if v})
