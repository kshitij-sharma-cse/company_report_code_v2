#!/usr/bin/env python3
"""One command for a company: builds every input table from raw records, then the report. The same script runs for every company.

    python run_company.py databricks --cik 0001587468 --name "Databricks, Inc." [--context-windows FILE.xlsx]
Everything specific to a company is in model_config/<company>.json. Order: input tables -> evidence by rule (evidence_tables.py,
evidence_build.py; --no-evidence skips) -> build_report.py -> with --context-windows, recall check and evidence workbook.
A config key a step needs but the company does not have (peers, k401_plan ...) is reported as "not configured".
"""
from __future__ import annotations

import argparse
import json
import os
import re
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
ap.add_argument("--no-evidence", action="store_true", help="skip the rule-based evidence steps (evidence_tables.py, evidence_build.py)")
ap.add_argument("--workers", type=int, default=2, help="processes for evidence_tables.py")
ap.add_argument("--folders", nargs="*", default=None, help="dump folders of the company (default: config dump_folders, else <co> and every <co>_<word> with a manifest)")
ap.add_argument("--context-windows", default="", help="context-window workbook (tools/build_context_windows.py): also run evidence_recall.py and evidence_workbook.py")
args = ap.parse_args()
co, dumps = args.company, Path(args.dumps)


class NotConfigured(KeyError):
    def __str__(self):
        return f"not configured: model_config/{co}.json has no {self.args[0]!r}"


class Cfg(dict):                                     # a missing key names the config, not a bare KeyError
    def __missing__(self, k):
        raise NotConfigured(k)


cfg_path = HERE / "model_config" / f"{co}.json"
if not cfg_path.exists():
    sys.exit(f"not configured: {cfg_path} does not exist (copy another company's file and edit it)")
cfg = json.loads(cfg_path.read_text(encoding="utf-8"), object_hook=Cfg)
raw, out = dumps / f"{co}_gov" / "sources", HERE / "inputs" / co
missing = []
if args.folders is not None or cfg.get("dump_folders"):  # asked for by name: a folder without a manifest is reported, not dropped quietly
    asked = args.folders if args.folders is not None else cfg["dump_folders"]
    folders = [f for f in asked if (dumps / f"{f}_manifest.csv").exists()]
    missing += [f"dump folder {f}: no {dumps / f'{f}_manifest.csv'}, left out" for f in asked if f not in folders]
else:                                                # <co>, then <co>_ncsr, <co>_delta ...: one word after the company name
    folders = [co] + sorted(p.name[:-13] for p in dumps.glob(f"{co}_*_manifest.csv") if re.fullmatch(rf"{re.escape(co)}_[A-Za-z0-9]+", p.name[:-13]))
    folders = [f for f in folders if (dumps / f"{f}_manifest.csv").exists() and (dumps / f).is_dir()]
    if co not in folders:
        missing.append(f"dump folder {co}: no {dumps / co} with {dumps / f'{co}_manifest.csv'} (tools/download_filings.py {co} writes them)")
print("dump folders:", folders or "none", *(["MISSING", *missing] if missing else []), flush=True)
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
for name, fn in ([] if args.skip_inputs else steps):
    try:
        print(f"{name}: {fn()}", flush=True)
    except NotConfigured as e:                    # the company has no such assumption: nothing to build
        missing.append(f"{name}: {e}")
        print("MISSING", missing[-1], flush=True)
    except Exception as e:                        # a source this company does not have: the report keeps a placeholder for what depends on it
        missing.append(f"{name}: {type(e).__name__}: {e}")
        print("MISSING", missing[-1], flush=True)
link = out / "sources"
if not link.exists() and raw.exists():
    out.mkdir(parents=True, exist_ok=True)
    os.symlink(raw, link)
ev = HERE / "out" / co / "evidence"
ev_rows = ev / "evidence_rows.csv"


def run(name: str, argv: list) -> bool:
    print(f"{name}: {' '.join(argv)}", flush=True)
    rc = subprocess.run([sys.executable, *argv], cwd=HERE).returncode
    if rc:
        missing.append(f"{name}: exit {rc}")
        print("MISSING", missing[-1], flush=True)
    return rc == 0


if not args.no_evidence:                          # numbers read by rule from the filings, before the report so it can use them
    ev_rows.unlink(missing_ok=True)               # a failed step must not leave an older run's rows for the report
    if not folders:
        missing.append("evidence: no dump folder with a manifest")
        print("MISSING", missing[-1], flush=True)
    elif run("evidence tables", [str(HERE / "evidence_tables.py"), co, "--folders", *folders, "--workers", str(args.workers), "--dumps", str(dumps)]):
        run("evidence build", [str(HERE / "evidence_build.py"), co])
company = cfg.get("company", {})
cmd = [sys.executable, str(HERE / "build_report.py"), co, "--cik", args.cik, "--name", args.name, "--formd-db", args.formd_db, "--model-config", str(cfg_path),
       "--employee-company", company.get("employee_company", ""), "--spv-key", company.get("spv_key", "")]
APP = HERE.parent / "coref_verifier"


TARGETED = next((p for p in (HERE.parent / "Targeted_pdf", HERE.parents[1] / "Resources" / "Targeted_pdf") if p.exists()), HERE.parent / "Targeted_pdf")


def has_mentions(f: str) -> bool:                 # build_report.py prepares an extra folder with prepare_targeted.py, which needs both
    return any((APP / "data" / "companies").glob(f"{f}__*_mentions.meta.json")) or (       # (same Targeted_pdf as prepare_targeted.py)
        (TARGETED / "dumps" / f"{f}_manifest.csv").exists() and any(TARGETED.glob(f"*/out/{f}_*/{f}_mentions.csv")))


for f in [x for x in folders if x != co]:
    if has_mentions(f):
        cmd += ["--extra-folder", f]
    else:                                          # the report's model-read facts then lack this form set: say so at the end
        missing.append(f"report: {f} left out (no company-finder mentions run; run company_finder, then prepare_targeted.py {f})")
        print("MISSING", missing[-1], "- used by the evidence steps only", flush=True)
if not args.no_evidence and ev_rows.exists():
    cmd += ["--evidence-rows", str(ev_rows)]
if np_csv.exists():
    cmd += ["--nport-csv", str(np_csv)]
if cfg.get("split_ratio") and cfg.get("split_date"):
    cmd += ["--split", f"{cfg['split_date']}:{cfg['split_ratio']}"]
elif cfg.get("split_ratio"):
    missing.append(f"stock split: not configured: model_config/{co}.json has split_ratio but no 'split_date'")
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
rc = subprocess.run(cmd).returncode
if rc:
    missing.append(f"report: exit {rc}")
if args.context_windows:                          # recall check: every number on a line naming the company must be in the evidence rows
    if not Path(args.context_windows).exists():
        missing.append(f"recall: {args.context_windows} not found (tools/build_context_windows.py writes it)")
    elif not ev_rows.exists():
        missing.append("recall: no evidence rows (evidence steps skipped or failed)")
    elif run("evidence recall", [str(HERE / "evidence_recall.py"), co, str(Path(args.context_windows).resolve())]):
        run("evidence workbook", [str(HERE / "evidence_workbook.py"), co, cfg.get("name") or args.name, str(ev)])
print("not built:", missing or "none", flush=True)
sys.exit(rc)
