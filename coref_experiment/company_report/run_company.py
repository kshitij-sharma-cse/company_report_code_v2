#!/usr/bin/env python3
"""One command for a company: builds every input table from raw records, then the report. The same script runs for every company.

    python run_company.py databricks --cik 0001587468 --name "Databricks, Inc."
Everything specific to a company is in model_config/<company>.json.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import build_inputs as a
import build_inputs_market as m
import build_inputs_more as b
import build_inputs_records as r
import build_inputs_text as c

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
ap = argparse.ArgumentParser()
ap.add_argument("company")
ap.add_argument("--cik", required=True)
ap.add_argument("--name", required=True)
ap.add_argument("--dumps", default=str(ROOT / "dumps"))
ap.add_argument("--formd-db", default=str(ROOT / "input" / "formd" / "formd.duckdb"))
ap.add_argument("--nport-db", default=str(Path.home() / "Downloads" / "nport_team.duckdb"))
ap.add_argument("--no-llm", action="store_true")
ap.add_argument("--repair", action="store_true")
ap.add_argument("--skip-inputs", action="store_true", help="reuse the input tables already built")
args = ap.parse_args()
co, dumps = args.company, Path(args.dumps)
cfg = json.loads((HERE / "model_config" / f"{co}.json").read_text(encoding="utf-8"))
raw, out = dumps / f"{co}_gov" / "sources", HERE / "inputs" / co
folders = [f for f in (co, f"{co}_ncsr") if (dumps / f"{f}_manifest.csv").exists()]
np_csv = dumps / f"{co}_nport" / "nport_holdings.csv"
facts = HERE / "out" / co / "facts"
steps = [("N-PORT loans", lambda: a.nport(args.nport_db, cfg, out)), ("N-PORT marks", lambda: a.marks(str(np_csv), cfg, out)), ("FPDS", lambda: a.fpds(raw, out)), ("USPTO", lambda: a.uspto(raw, out)),
         ("Form D", lambda: a.formd(raw, args.formd_db, cfg, out)), ("peers", lambda: a.peers(raw, cfg, out)), ("peer credit", lambda: c.peer_tables(raw, cfg, out)), ("lenders", lambda: b.lenders(raw, cfg, out)),
         ("S&P", lambda: b.sp_tables(raw, out)), ("H-1B", lambda: b.h1b(dumps / "raw_h1b", raw / "manifest.csv", cfg, out)), ("Form 5500", lambda: b.form5500(dumps / "employee_docs" / "raw", cfg, out)),
         ("IRS limits", lambda: b.irs_limits(raw, out)), ("France", lambda: b.france(raw, out)), ("U.K. accounts", lambda: (b.ocr_pdfs(raw, out / "ocr"), b.uk_accounts(out / "ocr", out))[1]),
         ("company statements", lambda: c.figures(dumps, folders, cfg, out)), ("market tables", lambda: {**m.fedramp(raw, cfg, out), **m.usaspending(raw, out), **m.nsf(raw, out), **m.mentions(raw, cfg, out), **m.holders(np_csv, cfg, out), **m.dated_lots(facts, cfg, out), **m.acquisitions(facts, cfg, out)}),
         ("series prices", lambda: c.series_prices(facts, out)), ("fund multiples", lambda: c.multiples(dumps, folders, cfg, out))]
steps += [("clinical trials", lambda: r.trials(raw, out)), ("FDA", lambda: r.fda(raw, out)), ("CMS Open Payments", lambda: r.open_payments(raw, out)), ("federal awards", lambda: r.awards(raw, out)),
          ("Canada grants", lambda: r.canada(raw, out)), ("PPP", lambda: r.ppp(dumps / "employee_docs" / "data", cfg, out)), ("agency statements", lambda: r.statements(raw, cfg, out)), ("Form D notices", lambda: r.formd_notices(raw, out))]
missing = []
for name, fn in ([] if args.skip_inputs else steps):
    try:
        print(f"{name}: {fn()}", flush=True)
    except Exception as e:                        # a source this company does not have: the report keeps a placeholder for what depends on it
        missing.append(f"{name}: {type(e).__name__}: {e}")
        print("MISSING", missing[-1], flush=True)
link = out / "sources"
if not link.exists() and raw.exists():
    out.mkdir(parents=True, exist_ok=True)
    os.symlink(raw, link)
cmd = [sys.executable, str(HERE / "build_report.py"), co, "--cik", args.cik, "--name", args.name, "--formd-db", args.formd_db, "--model-config", str(HERE / "model_config" / f"{co}.json"),
       "--employee-company", cfg["company"].get("employee_company", ""), "--spv-key", cfg["company"].get("spv_key", "")]
if (dumps / f"{co}_ncsr_manifest.csv").exists():
    cmd += ["--extra-folder", f"{co}_ncsr"]
if np_csv.exists():
    cmd += ["--nport-csv", str(np_csv)]
if cfg.get("split_ratio"):
    cmd += ["--split", f"{cfg['split_date']}:{cfg['split_ratio']}"]
if (dumps / "employee_docs" / "data").exists():
    cmd += ["--employee-dir", str(dumps / "employee_docs" / "data")]
needs = ("company_figures.csv", "credit_bdc_10k_positions.csv", "uk_companies_house_accounts.csv", "public_comps_sec_xbrl.csv", "nport_price_monthly.csv")
ready = all((out / "data" / n).exists() and len((out / "data" / n).read_text(encoding="utf-8").splitlines()) > 1 for n in needs) and "rr_last" in (out / "data" / "company_figures.csv").read_text(encoding="utf-8") if (out / "data").exists() else False
cmd += ["--records-dir", str(out / "data")]
if ready:
    cmd += ["--model-data", str(out)]
else:
    print("model off: the revenue, lender, peer or subsidiary inputs are not available for this company", flush=True)
for flag, on in (("--no-llm", args.no_llm), ("--repair", args.repair)):
    if on:
        cmd.append(flag)
print("not built:", missing or "none", flush=True)
sys.exit(subprocess.run(cmd).returncode)
