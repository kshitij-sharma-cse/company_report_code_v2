#!/usr/bin/env python3
"""Download the filings exhaustiveness.py lists as missing, except N-PORT exhibits and Form D (held as structured data). Writes dumps/<company>_delta and its manifest."""
import csv, re, sys, time, urllib.request
from pathlib import Path
UA = "projectx-filings-research testable485@gmail.com"
co = sys.argv[1]
here = Path(__file__).parent
dumps = here.parents[2] / "dumps"
rows = [r for r in csv.DictReader(open(here / "out" / co / "exhaustiveness" / "missing.csv", encoding="utf-8")) if not r["form"].startswith("NPORT") and r["form"] not in ("D", "D/A")]
dest = dumps / f"{co}_delta"
dest.mkdir(exist_ok=True)
body, fail = [], []
for r in rows:
    cik = re.search(r"CIK (\d+)", r["filer"])[1].lstrip("0")
    acc = r["accession"]
    for doc in r["docs"].split(";"):
        if len(doc) < 4:
            continue
        u = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}"
        form = re.sub(r"\W+", "-", r["form"])
        f = dest / form / f"{acc}_{doc}"
        f.parent.mkdir(exist_ok=True)
        try:
            if not f.exists():
                time.sleep(0.15)
                f.write_bytes(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=120).read())
            body.append([f"{form}/{f.name}", r["form"], r["file_date"], acc, r["filer"], u, "", "ok"])
        except Exception as e:
            fail.append((u, str(e)[:60]))
with open(dumps / f"{co}_delta_manifest.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh); w.writerow(["file", "form", "file_date", "accession", "filer", "sec_url", "index_url", "status"]); w.writerows(body)
print(len(rows), "filings", len(body), "documents", "failed", fail)
