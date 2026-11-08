#!/usr/bin/env python3
"""Company-finder input CSV (id, canonical_name, aliases, description) from model_config/<co>.json.

    python tools/make_companies_file.py outreach                      # -> coref_experiment/Targeted_pdf/run1/input/outreach_input.csv
    python tools/make_companies_file.py outreach --out companies.csv [--config model_config/outreach.json] [--name "..."]

* id: "C" + company.cik (10 digits), else <co>. canonical_name: the legal name -- company.legal_name, else --name (the
  --name run_company.py uses, "Get Heal, Inc."), else the config "name". aliases: the config "name" and
  company.aliases (the canonical name and case-insensitive repeats left out), joined by "|".
* An existing file is not overwritten: the row of the company (same id, or a canonical name or alias equal to one of
  its names) keeps its id, canonical_name and aliases (prepare_targeted.py copies canonical_name into every prepared
  filing) and only gets the description column filled; other rows and columns stay as they are. --force rewrites the
  file from the config.
* description: company.description (one or two sentences: what the company does, where, any former name). The
  disambiguation pass of company_finder.py --llm-disambiguate gives it to the model with each mention, so a look-alike
  organisation ("... Community Outreach Corp", a hospital board) can be told apart from the company. Without it the
  model judges from the name and excerpt alone: the file is still written, with a warning.
"""
import argparse
import csv
import json
import re
import sys
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[1] / "coref_experiment" / "company_report" / "model_config"
TARGETED = Path(__file__).resolve().parents[1] / "coref_experiment" / "Targeted_pdf"
COLUMNS = ["id", "canonical_name", "aliases", "description"]


def row(co: str, cfg: dict, legal: str = "") -> dict:
    company = cfg.get("company", {})
    short = re.sub(r"\s+", " ", str(cfg.get("name") or "")).strip()
    name = re.sub(r"\s+", " ", str(company.get("legal_name") or legal or short)).strip()
    if not name:
        sys.exit(f"{co}: the config has no \"name\"")
    seen, aliases = {name.lower()}, []
    for a in [short, *company.get("aliases", [])]:
        a = re.sub(r"\s+", " ", str(a or "")).strip()
        if a and a.lower() not in seen:
            seen.add(a.lower())
            aliases.append(a)
    if any("|" in a for a in aliases):
        sys.exit(f"{co}: an alias contains '|', the company-finder alias separator: {aliases}")
    cik = re.sub(r"\D", "", str(company.get("cik") or ""))
    description = re.sub(r"\s+", " ", str(company.get("description") or cfg.get("description") or "")).strip()
    return {"id": f"C{int(cik):010d}" if cik else co, "canonical_name": name, "aliases": "|".join(aliases), "description": description}


def merge(out: Path, r: dict) -> tuple:
    """Existing file: fill the description of the company's row, keep everything else."""
    key = lambda x: re.sub(r"\W+", " ", x or "").strip().lower()
    names = {key(x) for x in [r["canonical_name"], *r["aliases"].split("|")] if x}
    with open(out, newline="", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        columns, rows = list(rd.fieldnames or []), list(rd)
    if None in {k for x in rows for k in x}:
        sys.exit(f"{out}: a row has more fields than the header (unquoted comma?); fix it or rerun with --force")
    hit = [x for x in rows if (x.get("id") or "").strip() == r["id"] or (
        {key(x.get("canonical_name")), *(key(y) for y in (x.get("aliases") or "").split("|"))} & names)]
    if not hit:
        sys.exit(f"{out} exists and has no row for {r['canonical_name']} ({r['id']}): rerun with --force to rewrite it")
    if "description" not in columns:
        columns.append("description")
    for x in hit:
        if not (x.get("description") or "").strip():
            x["description"] = r["description"]
    print(f"{out} exists: kept its rows, id and names; description filled for {len(hit)} row(s) (--force rewrites it)")
    return columns, rows, hit[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("company", help="config name: model_config/<company>.json")
    ap.add_argument("--config", default="", help="default: coref_experiment/company_report/model_config/<company>.json")
    ap.add_argument("--out", default="", help="default: coref_experiment/Targeted_pdf/run1/input/<company>_input.csv")
    ap.add_argument("--name", default="", help="legal name for canonical_name when the config has no company.legal_name")
    ap.add_argument("--force", action="store_true", help="rewrite an existing file from the config (default: only fill its description)")
    a = ap.parse_args()
    p = Path(a.config) if a.config else CONFIG / f"{a.company}.json"
    if not p.exists():
        sys.exit(f"not configured: {p} does not exist")
    r = row(a.company, json.loads(p.read_text(encoding="utf-8")), a.name)
    out = Path(a.out) if a.out else TARGETED / "run1" / "input" / f"{a.company}_input.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    columns, rows = COLUMNS, [r]
    if out.exists() and not a.force:
        columns, rows, r = merge(out, r)
    tmp = out.with_suffix(out.suffix + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, columns)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(out)
    if not (r.get("description") or "").strip():
        print(f"WARNING {p}: no company.description; --llm-disambiguate will judge mentions from the name and excerpt "
              "alone, and look-alike organisations are more likely to be kept", file=sys.stderr)
    d = r.get("description") or ""
    print(f"wrote {out}: {r.get('canonical_name') or '-'} | aliases {r.get('aliases') or '-'} | description "
          f"{(d[:80] + '...') if len(d) > 80 else d or '-'}")


if __name__ == "__main__":
    main()
