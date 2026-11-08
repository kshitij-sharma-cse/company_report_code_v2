#!/usr/bin/env python3
"""Sort parsed rows by what proves them. N-PORT can confirm or flag a row; it never sets a value or a unit.

    A  tag         read from an iXBRL tag in the filing
    A  N-PORT      value (and shares where printed) equal a Form N-PORT row of the same date
    B  stated      column header and unit printed, no second source; or a share count printed with no amount
    C  review      no unit printed and no second source, per-share value out of range, or differs from N-PORT
The per-share range test needs an N-PORT mark within mark_window_months (model_config, default 3) of the row's date; with no mark that close the row stays B.
"""
import collections, csv, datetime as dt, json, re, sys
from pathlib import Path
csv.field_size_limit(10**8)
co = sys.argv[1]
HERE = Path(__file__).parent
ev = HERE / "out" / co / "evidence"
cfg = json.loads((HERE / "model_config" / f"{co}.json").read_text(encoding="utf-8"))
day_, ratio = cfg.get("split_date", "0"), cfg.get("split_ratio", 1)
win = cfg.get("mark_window_months", 3)     # an N-PORT mark further than this from the row's date says nothing about the row's price
P = list(csv.DictReader(open(ev / "parsed_rows.csv", encoding="utf-8")))
npf, mkf = HERE.parents[2] / "dumps" / f"{co}_nport" / "nport_holdings.csv", HERE / "inputs" / co / "data" / "nport_price_monthly.csv"
np_ = list(csv.DictReader(open(npf, encoding="utf-8"))) if npf.exists() else []     # a company no fund reports on N-PORT has none
by = collections.defaultdict(list)
for r in np_:
    by[r["report_date"]].append((float(r["value_usd"] or 0), float(r["shares"] or r["par_or_units"] or 0)))
marks = {r["month"]: float(r["median_price"]) for r in csv.DictReader(open(mkf, encoding="utf-8")) if r.get("median_price")} if mkf.exists() else {}


def f(x):
    return float(x) if x not in ("", None) else None


def iso(s):
    for fmt in ("%B %d, %Y", "%Y-%m-%d"):     # schedule headings print 'March 31, 2017'; iXBRL contexts give 2017-03-31
        try:
            return dt.datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def months(x):
    return int(x[:4]) * 12 + int(x[5:7])


out = []
for r in P:
    if r["problem"]:
        out.append({**r, "tier": "not a position" if r["problem"].startswith(("percent", "not a position")) else "C review", "why": r["problem"]})
        continue
    u = f(r["unit_factor"]) or 1
    q, c, v = f(r["qty"]), f(r["cost"]), f(r["value"])
    qty = q * (f(r.get("qty_factor")) or 1) if q else None
    val, cost = v * u if v else None, c * u if c else None
    d = iso(r["as_of"])
    r.update(qty_usd=qty, cost_usd=cost, value_usd=val, commitment_usd=f(r.get("commitment")) * u if f(r.get("commitment")) else None, report_date=d)
    why, tier = "", ""
    if r["method"] == "iXBRL tag":
        tier = "A tag"
    elif r["method"] == "pdf line":
        tier, why = "C review", "PDF line: columns must be read by eye"
    else:
        rows = by.get(d, [])
        tol = max(1.0, u * 0.51)
        vm = [x for x in rows if val and abs(x[0] - val) <= tol]
        qm = [x for x in vm if not qty or abs(x[1] - qty) <= max(1.0, x[1] * 0.0005)]
        if vm and qm:
            tier = "A N-PORT"
        elif val and rows and any(qty and abs(x[1] - qty) <= max(1.0, x[1] * 0.0005) for x in rows):
            tier, why = "C review", "same shares as an N-PORT row, different value"
        else:
            m = None
            if marks and val and qty and d:
                k = min(marks, key=lambda x: abs(months(x) - months(d)))
                m = marks[k] if abs(months(k) - months(d)) <= win else None
            loan = val and qty and 0.7 <= val / qty <= 1.3      # par and value: a loan, not shares
            if m and not loan and not m / 3 <= val / qty / (ratio if d < day_ else 1) <= m * 3:
                tier, why = "C review", "per-share value out of range"
            elif qty and val is None and cost is None and r["commitment_usd"] is None:
                tier = "B stated"                  # a printed share count and no amount: no money unit to prove
            elif r["unit"].startswith("no unit"):
                tier, why = "C review", "no unit printed, no second source"
            else:
                tier = "B stated"
    out.append({**r, "tier": tier, "why": why})
# a table with a row confirmed by N-PORT has its unit proven for the other rows of that table
proven = {(r["accession"], r["table"]) for r in out if r["tier"] == "A N-PORT"} | {(r["accession"], r["header"]) for r in out if r["tier"] == "A N-PORT"}
for r in out:
    if r["why"].startswith("no unit printed") and ((r["accession"], r["table"]) in proven or (r["accession"], r["header"]) in proven):
        r["tier"], r["why"] = "B stated", ""
        r["unit"] = "dollars, proven by another row of this filing with the same header"
head = list(P[0]) + ["qty_usd", "cost_usd", "value_usd", "commitment_usd", "report_date", "tier", "why"]
with open(ev / "evidence_rows.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, head, extrasaction="ignore")
    w.writeheader()
    w.writerows(out)
t = collections.Counter(r["tier"] for r in out)
print(t, collections.Counter(r["why"] for r in out).most_common(8))
print(collections.Counter((r["form"][:6], r["tier"]) for r in out if r["tier"]).most_common(16))
print({k: sum(1 for r in out if r["tier"] == "A N-PORT" and k in r["unit"]) for k in ("no unit", "ousand", "000")})
print(collections.Counter(r["filer"].split()[0] for r in out if r["tier"] == "C review").most_common(10))
