"""Input builders for the market view: FedRAMP, USAspending, NSF, 10-K mentions, fund holders, flows and price levels. Nothing is typed here."""
from __future__ import annotations

import ast
import collections
import csv
import json
import re
from pathlib import Path

from build_inputs import write
from build_inputs_text import plain


def fedramp(raw: Path, cfg: dict, out: Path) -> dict:
    d = json.load(open(raw / "fedramp" / "marketplace_data.json", encoding="utf-8"))["data"]
    key = cfg["company"]["name_regex"]
    body = []
    for p in d["Products"]:
        if re.search(key, p.get("csp", "") + " " + p.get("cso", ""), re.I):
            ag = p.get("agency_authorizations") or []
            sv = p.get("all_others") or []
            ag, sv = (ast.literal_eval(x) if isinstance(x, str) else x for x in (ag, sv))
            body.append([p["cso"], p["impact_level"], p["status"], p["auth_date"][:10], p["reuse"], "; ".join(ag), len(sv)])
    return {"fedramp.csv": write(out, "fedramp.csv", ["offering", "impact_level", "status", "authorized", "reuse", "agencies", "services"], body)}


def usaspending(raw: Path, out: Path) -> dict:
    res = {}
    for name, f in (("recipient", "search_recipient_databricks_contracts.json"), ("keyword", "search_keyword_databricks_contracts.json")):
        p = raw / "usaspending" / f
        if p.exists():
            for r in json.load(open(p, encoding="utf-8")).get("results", []):
                res[(name, r["Award ID"])] = [name, r["Award ID"], r.get("Recipient Name"), r.get("Awarding Agency"), r.get("Start Date"), r.get("Award Amount"), (r.get("Description") or "")[:110]]
    return {"usaspending_awards.csv": write(out, "usaspending_awards.csv", ["search_basis", "award_id", "recipient", "agency", "start", "award_amount_usd", "description"], list(res.values()))}


def nsf(raw: Path, out: Path) -> dict:
    body = []
    for p in sorted((raw / "nsf").glob("*.json")):
        d = json.load(open(p, encoding="utf-8"))
        for a in (d.get("response", {}).get("award") or [d]):
            body.append([a.get("id") or p.stem.rsplit("_", 1)[-1], a.get("title"), a.get("awardeeName"), a.get("startDate"), a.get("expDate"), a.get("fundsObligatedAmt")])
    return {"nsf_awards.csv": write(out, "nsf_awards.csv", ["award", "title", "awardee", "start", "end", "obligated_usd"], body)}


def mentions(raw: Path, cfg: dict, out: Path) -> dict:
    """Sentences naming the company in the 10-Ks of operating companies (files without a lender schedule row)."""
    key = cfg["company"]["name_regex"]
    body = []
    for p in sorted((raw / "sec_10k").glob("*.htm")):
        t = p.read_text(encoding="utf-8", errors="ignore")
        if "InvestmentIdentifierAxis" in t:
            continue
        name = re.search(r'name="dei:EntityRegistrantName"[^>]*>(.*?)<', t)
        co = name[1] if name else p.name.split("_")[0]
        for s in re.split(r"(?<=[.;])\s+", plain(p)):
            if re.search(key, s, re.I) and 30 < len(s) < 400:
                body.append([re.sub(r"<[^>]+>", "", co), s])
    return {"company_mentions_10k.csv": write(out, "company_mentions_10k.csv", ["company", "sentence"], body)}


def holders(nport_csv: Path, cfg: dict, out: Path) -> dict:
    rows = list(csv.DictReader(open(nport_csv, encoding="utf-8", newline="")))
    f = lambda x: float(x) if x not in ("", None) else 0.0                                         # noqa: E731
    fund = lambda r: r["series_id"] if r["series_id"] not in ("", "N/A") else r["cik"]             # noqa: E731
    last: dict = {}
    for r in rows:
        last[fund(r)] = max(last.get(fund(r), ""), r["report_date"])
    agg: dict = {}
    newest = max(last.values())
    cut = f"{int(newest[:4]) - (1 if int(newest[5:7]) <= 3 else 0)}-{(int(newest[5:7]) - 4) % 12 + 1:02d}-01"     # funds that still report: the last four months
    for r in rows:
        k = fund(r)
        if r["report_date"] != last[k] or last[k] < cut:
            continue
        a = agg.setdefault(k, {"house": r["fund_house"], "fund": r["series"] or r["registrant"], "date": r["report_date"], "value": 0.0, "pct": 0.0, "kinds": collections.Counter(), "acc": r["accession"]})
        a["value"] += f(r["value_usd"])
        a["pct"] += f(r["pct_net_assets"])
        a["kinds"][r["position_type"]] += f(r["value_usd"])
    body = sorted(([a["house"], a["fund"], a["date"], round(a["value"]), round(a["pct"], 4), a["kinds"].most_common(1)[0][0], a["acc"], round(a["kinds"]["equity"]), round(a["kinds"]["funded_loan"]), round(a["value"] - a["kinds"]["equity"] - a["kinds"]["funded_loan"])] for a in agg.values()), key=lambda r: -r[3])
    n = write(out, "nport_holders.csv", ["fund_house", "fund", "report_date", "value_usd", "pct_net_assets", "exposure", "accession", "equity_usd", "loan_usd", "other_usd"], body)
    day, ratio = cfg.get("split_date", "0"), cfg.get("split_ratio", 1)
    by = collections.defaultdict(dict)                 # last report of each quarter per fund: shares and mark
    for r in rows:
        if r["position_type"] != "equity" or not f(r["shares"]):
            continue
        q = f"{r['report_date'][:4]}Q{(int(r['report_date'][5:7]) + 2) // 3}"
        a = 1 / ratio if r["report_date"] < day else 1
        e = by[fund(r)].setdefault(q, {"date": "", "sh": 0.0, "val": 0.0})
        if r["report_date"] > e["date"]:
            e.update(date=r["report_date"], sh=0.0, val=0.0)
        if r["report_date"] == e["date"]:
            e["sh"] += f(r["shares"]) / a
            e["val"] += f(r["value_usd"])
    net = collections.Counter()
    for k, qs in by.items():
        prev = 0.0                             # a fund's first report counts as an initiation
        for q in sorted(qs):
            net[q] += (qs[q]["sh"] - prev) * (qs[q]["val"] / qs[q]["sh"] if qs[q]["sh"] else 0)
            prev = qs[q]["sh"]
    m = write(out, "nport_flows.csv", ["quarter", "net_buying_usd"], [[q, round(v)] for q, v in sorted(net.items())])
    months: dict = collections.defaultdict(collections.Counter)
    houses: dict = collections.defaultdict(set)
    for r in rows:
        if r["position_type"] == "equity" and f(r["filed_price_per_share"]):
            p = round(f(r["filed_price_per_share"]) * (1 / ratio if r["report_date"] < day else 1), 2)
            months[r["report_date"][:7]][p] += 1
            houses[(r["report_date"][:7], p)].add(r["fund_house"])
    levels: dict = {}
    for mo in sorted(months):
        top = max(months[mo].items(), key=lambda kv: (len(houses[(mo, kv[0])]), kv[0]))[0]   # the mark most fund houses used that month; a tie goes to the higher, newer mark
        e = levels.setdefault(top, {"first": mo, "last": mo, "n": 0, "max": 0})
        e["last"], e["n"], e["max"] = mo, e["n"] + 1, max(e["max"], len(houses[(mo, top)]))
    levels = {p: e for p, e in levels.items() if e["max"] >= 3}
    l = write(out, "price_levels.csv", ["price", "first_month", "last_month", "months"], [[p, e["first"], e["last"], e["n"]] for p, e in sorted(levels.items(), key=lambda t: t[1]["first"])])
    return {"nport_holders.csv": n, "nport_flows.csv": m, "price_levels.csv": l}


def dated_lots(facts_dir: Path, cfg: dict, out: Path) -> dict:
    """Purchases in fund schedules: shares, cost and acquisition date. Per-share cost is split-adjusted and must sit within 3x of the funds' mark of that time."""
    marks = {r["month"]: float(r["median_price"]) for r in csv.DictReader(open(out / "data" / "nport_price_monthly.csv", encoding="utf-8"))}
    day, ratio = cfg.get("split_date", "0"), cfg.get("split_ratio", 1)
    near = lambda m: marks[min(marks, key=lambda x: abs(int(x[:4]) * 12 + int(x[5:]) - int(m[:4]) * 12 - int(m[5:7])))]   # noqa: E731
    seen: dict = {}
    for p in facts_dir.glob("*.json"):
        for h in json.load(open(p, encoding="utf-8")).get("holdings", []):
            d, sh, c = str(h.get("acquisition_date") or ""), h.get("shares"), h.get("cost_usd")
            if h.get("check") != "ok" or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) or not sh or not c or sh < 100:
                continue
            seen_on = str(h.get("as_of_date") or "")           # the share count is on the basis of the report date, not the purchase date
            adj = c / sh / (ratio if (seen_on or d) < day else 1)
            if not 0.75 * near(d) <= adj <= 1.35 * near(d):
                continue
            s = re.search(r"\bSer(?:ies|\.)?\s+([A-Z])\b", str(h.get("security") or ""))
            k = (d, s[1] if s else "other", round(adj, 2))
            e = seen.setdefault(k, {"n": 0, "holder": h.get("holder"), "shares": sh, "cost": c})
            e["n"] += 1
    body = sorted([k[0], k[1], k[2], e["shares"], e["cost"], e["holder"], e["n"]] for k, e in seen.items())
    return {"dated_lots.csv": write(out, "dated_lots.csv", ["acquired", "series", "cost_per_share_split_adj", "shares", "cost_usd", "holder", "rows"], body)}


def acquisitions(facts_dir: Path, cfg: dict, out: Path) -> dict:
    key, seen, body = cfg["company"]["name_regex"], set(), []
    for p in sorted(facts_dir.glob("*.json")):
        for f in json.load(open(p, encoding="utf-8")).get("facts", []):
            t = f.get("value_text") or ""
            if f.get("check") == "ok" and re.search(r"(^|\b)" + key + r"\b[^.]{0,40}(agreed to acquire|acquired|to acquire|acquisition of)", t, re.I):
                k = re.sub(r"\W+", " ", t.lower())[:60]
                if k not in seen:
                    seen.add(k)
                    body.append([f.get("as_of_date") or "", t, f.get("evidence") or "", p.stem[:30]])
    return {"acquisitions.csv": write(out, "acquisitions.csv", ["as_of", "statement", "quote", "filing"], sorted(body))}
