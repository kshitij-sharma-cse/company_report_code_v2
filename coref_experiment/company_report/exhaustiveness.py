#!/usr/bin/env python3
"""Compare every EDGAR full-text hit for a company name with the filings we hold. No model is used.

    python exhaustiveness.py databricks --term Databricks --term "Data Bricks" --folders databricks databricks_ncsr databricks_nport
Writes out/<company>/exhaustiveness/: edgar_hits.csv (one row per filing), missing.csv, held_not_in_edgar.csv, by_form.csv.
"""
from __future__ import annotations

import argparse
import calendar
import collections
import csv
import datetime as dt
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

UA = "projectx-filings-research testable485@gmail.com"
URL = "https://efts.sec.gov/LATEST/search-index"
ap = argparse.ArgumentParser()
ap.add_argument("company")
ap.add_argument("--term", action="append", required=True)
ap.add_argument("--folders", nargs="+", required=True)
ap.add_argument("--start", default="2001-01-01")
ap.add_argument("--end", default=dt.date.today().isoformat())
ap.add_argument("--dumps", default=str(Path(__file__).parents[3] / "dumps"))
a = ap.parse_args()
out = Path(__file__).parent / "out" / a.company / "exhaustiveness"
out.mkdir(parents=True, exist_ok=True)


def get(params: dict) -> dict:
    u = URL + "?" + urllib.parse.urlencode(params)
    for i in range(6):
        try:
            time.sleep(0.13)
            return json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=60))
        except Exception:
            time.sleep(2 * (i + 1))
    raise RuntimeError("EDGAR did not answer: " + u)


hits: dict = {}
queries = truncated = 0


def window(term: str, s: dt.date, e: dt.date) -> None:
    """All hits in a date window. EDGAR returns at most 10,000 hits per search, so a full window is split in two."""
    global queries, truncated
    base = {"q": f'"{term}"', "dateRange": "custom", "startdt": s.isoformat(), "enddt": e.isoformat()}
    first = get({**base, "from": 0})
    queries += 1
    total = first["hits"]["total"]["value"]
    if total >= 10000:
        if s == e:
            truncated += 1
        else:
            mid = s + (e - s) // 2
            window(term, s, mid)
            window(term, mid + dt.timedelta(days=1), e)
            return
    got, page = 0, first
    while True:
        rows = page["hits"]["hits"]
        if not rows:
            break
        for h in rows:
            x = h["_source"]
            k = x.get("adsh") or h["_id"].split(":")[0]
            e_ = hits.setdefault(k, {"accession": k, "form": x.get("form") or x.get("file_type"), "file_date": x.get("file_date"), "filer": "; ".join(x.get("display_names") or [])[:120], "docs": set(), "terms": set()})
            e_["docs"].add(h["_id"].split(":")[-1])
            e_["terms"].add(term)
        got += len(rows)
        if got >= min(total, 10000):
            break
        page = get({**base, "from": got})
        queries += 1


for term in a.term:
    d = dt.date.fromisoformat(a.start)
    last = dt.date.fromisoformat(a.end)
    while d <= last:
        z = min(dt.date(d.year, 12, 31), last)
        window(term, d, z)
        print(term, d.year, "filings so far", len(hits), flush=True)
        d = z + dt.timedelta(days=1)

held: dict = {}
for f in a.folders:
    p = Path(a.dumps) / f"{f}_manifest.csv"
    if p.exists():
        for r in csv.DictReader(open(p, encoding="utf-8", newline="")):
            held.setdefault(r["accession"], {"form": r["form"], "file_date": r["file_date"], "filer": r["filer"], "folder": f})
    else:
        for x in (Path(a.dumps) / f).rglob("*"):
            m = re.match(r"(\d{10}-\d{2}-\d{6})", x.name)
            if m:
                held.setdefault(m[1], {"form": "", "file_date": "", "filer": "", "folder": f})

head = ["accession", "form", "file_date", "filer", "docs", "terms"]
row = lambda v: [v["accession"], v["form"], v["file_date"], v["filer"], ";".join(sorted(v["docs"]))[:200], ";".join(sorted(v["terms"]))]    # noqa: E731
with open(out / "edgar_hits.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh); w.writerow(head); w.writerows(row(v) for v in sorted(hits.values(), key=lambda v: str(v["file_date"])))
miss = [v for k, v in hits.items() if k not in held]
with open(out / "missing.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh); w.writerow(head); w.writerows(row(v) for v in sorted(miss, key=lambda v: str(v["file_date"])))
only = {k: v for k, v in held.items() if k not in hits}
with open(out / "held_not_in_edgar.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh); w.writerow(["accession", "form", "file_date", "filer", "folder"]); w.writerows([k, v["form"], v["file_date"], v["filer"], v["folder"]] for k, v in sorted(only.items()))
fh_ = collections.Counter(str(v["form"]) for v in hits.values())
fm = collections.Counter(str(v["form"]) for v in miss)
with open(out / "by_form.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh); w.writerow(["form", "edgar_filings", "held", "missing"]); w.writerows([f, n, n - fm[f], fm[f]] for f, n in fh_.most_common())
print(json.dumps({"edgar_filings": len(hits), "held": len(held), "edgar_and_held": len(hits) - len(miss), "missing": len(miss), "held_not_in_edgar": len(only), "queries": queries, "days_over_limit": truncated}))
