#!/usr/bin/env python3
"""Build the model's input tables from raw records, with no value typed by hand. One function per source; each writes one CSV to --out/data.

    python build_inputs.py --raw RAWDIR --nport-db FILE --formd-db FILE --config model_config/company.json --out inputs/company
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import json
import re
import statistics
import xml.etree.ElementTree as ET
from pathlib import Path

import duckdb


def write(out: Path, name: str, header: list, body: list) -> int:
    (out / "data").mkdir(parents=True, exist_ok=True)
    with open(out / "data" / name, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(body)
    return len(body)


def nport(db: str, cfg: dict, out: Path) -> dict:
    key = cfg["company"]["match"]
    con = duckdb.connect(db, read_only=True)
    q = f"""SELECT h.report_date, h.filing_date, h.fund_house, h.series_name, h.fund_key, h.cik, h.ACCESSION_NUMBER, h.ISSUER_NAME, h.ISSUER_TITLE, t.MATURITY_DATE, t.COUPON_TYPE,
                   t.ANNUALIZED_RATE, h.BALANCE, h.CURRENCY_VALUE, t.IS_DEFAULT, t.ARE_ANY_INTEREST_PAYMENT, h.FAIR_VALUE_LEVEL
            FROM holdings h JOIN debt_terms t ON h.HOLDING_ID = t.HOLDING_ID AND h.quarter = t.quarter
            WHERE h.ASSET_CAT = 'LON' AND (upper(h.ISSUER_NAME) LIKE '%{key}%' OR upper(h.ISSUER_TITLE) LIKE '%{key}%') ORDER BY 1, 5"""
    body, seen = [], set()
    for r in con.execute(q).fetchall():
        rate = float(r[11] or 0)
        par = float(r[12] or 0)
        k = (r[0], r[4], r[8], par, float(r[13] or 0))
        if k in seen:
            continue
        seen.add(k)
        body.append([r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10], rate, par, r[13], round(float(r[13] or 0) / par * 100, 3) if par else "",
                     "funded" if rate > 6 and par > 0 else "unfunded / fee", r[14], r[15], r[16]])
    n = write(out, "nport_loans.csv", ["report_date", "filing_date", "fund_house", "series_name", "fund_key", "cik", "accession", "issuer_name", "issuer_title", "maturity", "coupon_type",
                                       "annualized_rate_pct", "par_usd", "fair_value_usd", "price_pct_of_par", "status", "is_default", "interest_in_arrears", "fair_value_level"], body)
    return {"nport_loans.csv": n}


def marks(nport_csv: str, cfg: dict, out: Path) -> dict:
    by = collections.defaultdict(list)
    day, ratio = cfg.get("split_date", "0000"), cfg.get("split_ratio", 1)
    with open(nport_csv, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["position_type"] == "equity" and r["filed_price_per_share"]:
                r["adj"] = float(r["filed_price_per_share"]) / (ratio if r["report_date"] < day else 1)
                by[r["report_date"][:7]].append(r)
    body = []
    for m in sorted(by):
        p = [x["adj"] for x in by[m]]
        body.append([m, len(p), len({x["series_id"] or x["cik"] for x in by[m]}), round(statistics.median(p), 4), round(min(p), 4), round(max(p), 4)])
    return {"nport_price_monthly.csv": write(out, "nport_price_monthly.csv", ["month", "positions", "funds", "median_price", "min_price", "max_price"], body)}


def fpds(raw: Path, out: Path) -> dict:
    ns = "{https://www.fpds.gov/FPDS}"
    seen, body = set(), []
    for p in sorted((raw / "fpds").glob("*.xml")):
        for e in ET.parse(p).getroot().iter("{http://www.w3.org/2005/Atom}content"):
            for rec in e:
                piid = next((x.text for x in rec.iter(ns + "PIID")), "")
                mod = next((x.text for x in rec.iter(ns + "modNumber")), "")
                tx = next((x.text for x in rec.iter(ns + "transactionNumber")), "")
                k = (piid, mod, tx)
                if k in seen:
                    continue
                seen.add(k)
                g = lambda t: next((x.text for x in rec.iter(ns + t) if x.text), "")       # noqa: E731
                body.append([piid, mod, rec.tag.replace(ns, ""), g("signedDate")[:10], g("obligatedAmount") or 0, g("baseAndAllOptionsValue"), g("descriptionOfContractRequirement"), g("vendorName")])
    body.sort(key=lambda r: r[3])
    return {"fpds_actions.csv": write(out, "fpds_actions.csv", ["piid", "mod", "kind", "signed_date", "obligated_usd", "base_and_all_options_usd", "description", "vendor"], body)}


def uspto(raw: Path, out: Path) -> dict:
    seen, body = set(), []
    for p in sorted((raw / "uspto").glob("*.json")):
        for r in (json.load(open(p, encoding="utf-8")).get("successResponse") or {}).get("data") or []:
            k = (r["reelNumber"], r["frameNumber"])
            if k in seen:
                continue
            seen.add(k)
            body.append([r["reelNumber"], r["frameNumber"], r["conveyance"], r.get("assignorExecutionDate") or "", "; ".join(a["assignorName"] for a in r.get("assignors") or []),
                         "; ".join(r.get("assignees") or []), r.get("noOfProperties", 0)])
    return {"uspto_assignments.csv": write(out, "uspto_assignments.csv", ["reel", "frame", "conveyance", "execution_date", "assignors", "assignees", "properties"], body)}


def formd(raw: Path, db: str, cfg: dict, out: Path) -> dict:
    cik = cfg["company"]["cik"]
    bc = {}
    for p in (raw / "sec_form_d").glob("*.xml"):
        t = p.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"<isBusinessCombinationTransaction>\s*(true|false)", t, re.I)
        bc[p.name[:20]] = m[1].lower() if m else "false"
    con = duckdb.connect(db, read_only=True)
    rows = con.execute("""SELECT file_num, first_accession, last_accession, first_sale_date, first_filing_date, last_filing_date, total_amount_sold FROM offerings
                          WHERE cik = ? ORDER BY first_filing_date""", [cik]).fetchall()
    body, seen = [], set()
    for r in rows:
        k = r[1]
        if k in seen:
            continue
        seen.add(k)
        body.append([cik, r[1], str(r[4])[:10], str(r[5])[:10], str(r[3])[:10], r[6], bc.get(r[2], bc.get(r[1], "false")), "company"])
    return {"FORMD_offerings.csv": write(out, "FORMD_offerings.csv", ["cik", "offering_id", "first_filed", "last_filed", "date_of_first_sale", "amount_sold_usd", "business_combination", "role"], body)}


def _ttm(facts: dict, names: list):
    """Trailing twelve months to the latest period end, from durations in XBRL company facts."""
    for name in names:
        c = facts.get(name)
        if not c:
            continue
        rows = {}
        for x in c["units"].get("USD", []):
            if "start" in x:
                a, z = dt.date.fromisoformat(x["start"]), dt.date.fromisoformat(x["end"])
                rows[(a, z)] = x["val"]
        if rows:
            yield name, rows


def peers(raw: Path, cfg: dict, out: Path) -> dict:
    body = []
    for p in cfg["company"]["peers"]:
        f = json.load(open(next((raw / "sec_xbrl").glob(f"*_{p}.json")), encoding="utf-8"))["facts"]["us-gaap"]
        rev = dict(_ttm(f, ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"]))
        gp = dict(_ttm(f, ["GrossProfit"]))
        cost = dict(_ttm(f, ["CostOfRevenue", "CostOfGoodsAndServicesSold"]))

        def ttm(rows, end):
            year = [(k, v) for k, v in rows.items() if k[1] == end and 350 <= (k[1] - k[0]).days <= 380]
            if year:
                return year[0][1]
            ytd = [(k, v) for k, v in rows.items() if k[1] == end and (k[1] - k[0]).days < 350]
            if not ytd:
                return None
            k, v = max(ytd, key=lambda t: (t[0][1] - t[0][0]).days)
            fy = [(a, b) for a, b in rows if b == k[0] - dt.timedelta(days=1) and 350 <= (b - a).days <= 380]
            prior = [(a, b) for a, b in rows if (b - end).days in range(-372, -358) and abs((a - k[0]).days - 0) in range(350, 380)]
            if not fy or not prior:
                return None
            return rows[fy[0]] + v - rows[prior[0]]

        rv = max(rev.values(), key=lambda r: max(k[1] for k in r))
        end = max(k[1] for k in rv)
        tr = ttm(rv, end)
        tg = ttm(max(gp.values(), key=len), end) if gp else None
        if tg is None and cost:
            c_ = ttm(max(cost.values(), key=len), end)
            tg = tr - c_ if c_ is not None and tr else None
        body.append([p, end, tr, tg, round(tg / tr, 4) if tr and tg else ""])
    return {"public_comps_sec_xbrl.csv": write(out, "public_comps_sec_xbrl.csv", ["company", "fiscal_q_end", "ttm_rev_usd", "ttm_gross_profit_usd", "ttm_gross_margin"], body)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True)
    ap.add_argument("--nport-db", required=True)
    ap.add_argument("--nport-csv", required=True)
    ap.add_argument("--formd-db", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    cfg, raw, out = json.load(open(a.config, encoding="utf-8")), Path(a.raw), Path(a.out)
    res = {}
    res.update(nport(a.nport_db, cfg, out))
    res.update(marks(a.nport_csv, out))
    res.update(fpds(raw, out))
    res.update(uspto(raw, out))
    res.update(formd(raw, a.formd_db, cfg, out))
    res.update(peers(raw, cfg, out))
    print(res)
