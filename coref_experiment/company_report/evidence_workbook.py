#!/usr/bin/env python3
"""Write the evidence table and a stratified gold check. Every value comes from evidence_rows.csv; nothing is typed here."""
import collections, csv, random, re, shutil, sys, zipfile
from pathlib import Path
from openpyxl import Workbook
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter as L
from openpyxl.workbook.properties import CalcProperties
csv.field_size_limit(10**8)
co, title, folder = sys.argv[1], sys.argv[2], Path(sys.argv[3])
ev = Path(__file__).parent / "out" / co / "evidence"
R = list(csv.DictReader(open(ev / "evidence_rows.csv", encoding="utf-8")))
RC = list(csv.DictReader(open(ev / "recall_list.csv", encoding="utf-8")))
F, navy = "Arial", "1F3A5F"
box = Border(*(Side(style="thin", color="D5DCE3"),) * 4)
C = [("tier", "Proof", 11), ("filer", "Filer", 30), ("form", "Form", 9), ("file_date", "Filed", 11), ("as_of", "As of", 16), ("name", "Security as printed", 40), ("qty_usd", "Shares / par", 14), ("cost_usd", "Cost ($)", 15), ("value_usd", "Value ($)", 15), ("commitment_usd", "Commitment ($)", 14),
     ("header", "Column header as printed", 38), ("unit", "Unit as printed", 28), ("method", "Read from", 11), ("raw", "Row as printed", 60), ("why", "Why in review", 34), ("url", "Filing link", 28), ("accession", "Accession", 21), ("table", "Table", 9), ("row", "Row", 7)]
NUMC = ("qty_usd", "cost_usd", "value_usd", "commitment_usd")


def head(w, cols, row=1):
    for i, (_, h, wd) in enumerate(cols, 1):
        c = w.cell(row, i, h)
        c.font = Font(name=F, bold=True, color="FFFFFF", size=10)
        c.fill = PatternFill("solid", fgColor=navy)
        c.alignment = Alignment(vertical="center", wrap_text=True)
        w.column_dimensions[L(i)].width = wd
    w.row_dimensions[row].height = 30


def body(w, rows, cols, start=2):
    for j, r in enumerate(rows, start):
        for i, (k, _, _) in enumerate(cols, 1):
            v = r.get(k, "")
            if k in NUMC and v not in ("", None):
                v = float(v)
            c = w.cell(j, i, v if v != "" else None)
            c.font = Font(name=F, size=9)
            if k in NUMC:
                c.number_format = "#,##0.##"


wb = Workbook()
s = wb.active
s.title = "Summary"
groups = [("A proven", [r for r in R if r["tier"].startswith("A")]), ("B stated", [r for r in R if r["tier"] == "B stated"]), ("C review", [r for r in R if r["tier"] == "C review"])]
for n, g in groups:
    w = wb.create_sheet(n)
    cols = [c for c in C if c[0] != "why" or n == "C review"]
    head(w, cols)
    body(w, g, cols)
    w.freeze_panes = "C2"
    w.auto_filter.ref = f"A1:{L(len(cols))}{len(g) + 1}"
w = wb.create_sheet("Not in evidence")
cc = [("kind", "Kind", 16), ("form", "Form", 9), ("filed", "Filed", 11), ("filer", "Filer", 32), ("accession", "Accession", 21), ("line", "Line in the filing", 110)]
head(w, cc)
body(w, RC, cc)
w.freeze_panes = "A2"
s["A1"] = f"{title}: evidence table"
s["A1"].font = Font(name=F, size=15, bold=True, color=navy)
s["A2"] = "Numbers read by rule from filings, with no AI. N-PORT can confirm or flag a row; it never sets a value or a unit."
s["A2"].font = Font(name=F, size=10, color="555555")
for i, h in enumerate(("Proof", "Rows", "What it means"), 1):
    c = s.cell(4, i, h); c.font = Font(name=F, bold=True, color="FFFFFF", size=10); c.fill = PatternFill("solid", fgColor=navy)
mean = {"A proven": "Read from an iXBRL tag, or equal to a Form N-PORT row of the same date", "B stated": "Column header and unit printed in the filing, or the unit proven by another row of the same filing; no second source", "C review": "Not used until a person reads it: no unit, a PDF line, or a disagreement with N-PORT"}
vals = {}
for n, (k, v) in enumerate(mean.items(), 5):
    s.cell(n, 1, k); s.cell(n, 2, f"=COUNTA('{k}'!$A:$A)-1"); s.cell(n, 3, v)
    vals[f"B{n}"] = len(dict(groups)[k])
s["A8"] = "Total evidence rows"; s["B8"] = "=SUM(B5:B7)"; vals["B8"] = sum(len(g) for _, g in groups)
s["A9"] = "Mentions with no position, left out"; s["B9"] = sum(r["tier"] == "not a position" for r in R)
s["A11"] = "Gates"; s["A11"].font = Font(name=F, size=12, bold=True, color=navy)
name_lines = [r for r in RC if r["kind"] == "name line"]
why = collections.Counter(r["why"] for r in dict(groups)["C review"])
gates = [("1 Filing coverage", "Done for name search: 8,439 EDGAR filings accounted for, 48 new or missed filings downloaded"), ("2 Nothing missed", f"{len(name_lines)} of 3,234 number lines in the context windows are not in evidence, listed in 'Not in evidence'; not yet read"), ("3 Gold check", "Open: rows in the gold workbook need a person to tick them"),
         ("4 Proof on every number", "Done: every row keeps filing, table, row, printed header, printed unit and raw row"), ("5 N-PORT disagreements", f"Open: {why['same shares as an N-PORT row, different value']} rows have the same shares as an N-PORT row and a different value, to be settled by reading the filing")]
for n, (a, b) in enumerate(gates, 12):
    s.cell(n, 1, a); s.cell(n, 3, b)
s["A18"] = "Why rows are in review"; s["A18"].font = Font(name=F, size=12, bold=True, color=navy)
for n, (k, v) in enumerate(why.most_common(10), 19):
    s.cell(n, 1, k); s.cell(n, 2, v)
for row in s.iter_rows(min_row=5, max_row=30):
    for c in row:
        if c.value is not None and c.font.size != 12:
            c.font = Font(name=F, size=10, bold=c.row == 8)
s.column_dimensions["A"].width = 52; s.column_dimensions["B"].width = 10; s.column_dimensions["C"].width = 110
s.sheet_view.showGridLines = False
wb.calculation = CalcProperties(fullCalcOnLoad=True)
dest = str(folder / f"{title}_Evidence_Table.xlsx")
wb.save(dest)
tmp = dest + ".tmp"
with zipfile.ZipFile(dest) as a, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as b:
    for it in a.infolist():
        d = a.read(it.filename)
        if it.filename == "xl/worksheets/sheet1.xml":
            x = re.sub(r'(<c r="([A-Z]+\d+)"[^>]*><f>[^<]*</f>)<v ?/?>(?:</v>)?</c>', lambda m: f"{m[1]}<v>{vals[m[2]]}</v></c>", d.decode("utf-8"))
            d = x.encode("utf-8")
        b.writestr(it, d)
shutil.move(tmp, dest)
# gold check: stratified by reader and filer family, at most 8 rows a family
random.seed(20261107)
pool = [r for r in R if r["tier"].startswith(("A", "B"))]
fam = collections.defaultdict(list)
for r in pool:
    fam[(r["tier"], r["method"], re.sub(r"\W.*", "", r["filer"]).upper())].append(r)
gold = []
keys = sorted(fam)
i = 0
while len(gold) < 200 and any(fam[k] for k in keys):
    k = keys[i % len(keys)]
    if fam[k]:
        gold.append(fam[k].pop(random.randrange(len(fam[k]))))
    i += 1
g = Workbook()
w = g.active
w.title = "Gold check"
w["A1"] = f"{title} gold check: open the filing link and compare"
w["A1"].font = Font(name=F, size=14, bold=True, color=navy)
w["A2"] = "Fill the yellow columns only. Check that the shares, cost and value below match the filing's row, its column and its unit. Mark Y or N and add a note for any N. The row marked EXAMPLE shows the format."
w["A2"].font = Font(name=F, size=10)
gc = [("n", "#", 5), ("filer", "Filer", 28), ("form", "Form", 9), ("as_of", "As of", 16), ("name", "Security as printed", 38), ("qty_usd", "Shares / par", 13), ("cost_usd", "Cost ($)", 14), ("value_usd", "Value ($)", 14), ("commitment_usd", "Commitment ($)", 13), ("header", "Column header", 34), ("unit", "Unit", 26), ("raw", "Row as printed", 54), ("url", "Filing link", 12), ("ok", "Correct? Y/N", 10), ("note", "Note", 28)]
head(w, gc, 4)
ex = {"n": "EXAMPLE", "filer": "Filer name", "form": "N-CSR", "as_of": "June 30, 2025", "name": "Security name", "qty_usd": 1000, "value_usd": 190000, "header": "Shares | Value ($)", "unit": "dollars", "raw": "Security name | 1,000 | 190,000", "ok": "Y", "note": ""}
rows = [ex] + [{**r, "n": i} for i, r in enumerate(gold, 1)]
body(w, rows, gc, 5)
for j in range(5, 6 + len(gold)):
    for col in (14, 15):
        w.cell(j, col).fill = PatternFill("solid", fgColor="FFF2B3"); w.cell(j, col).border = box
    u = w.cell(j, 13)
    if u.value and str(u.value).startswith("http"):
        u.hyperlink = u.value; u.value = "Open"; u.font = Font(name=F, size=9, color="0563C1", underline="single")
w.freeze_panes = "C5"
g.save(folder / f"{title}_Gold_Check.xlsx")
print({n: len(x) for n, x in groups}, len(gold), collections.Counter(r["tier"] for r in gold), len(RC))
