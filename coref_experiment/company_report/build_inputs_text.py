"""Input builders that read filing text: company statements, series prices, fund revenue multiples, peer tables. Nothing is typed here."""
from __future__ import annotations

import calendar
import collections
import csv
import datetime as dt
import html
import json
import re
from pathlib import Path

from build_inputs import write

MONTHS = {m: i for i, m in enumerate(calendar.month_name) if m}


def plain(path: Path) -> str:
    t = path.read_text(encoding="utf-8", errors="ignore")
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t)).replace("\xa0", " ").replace("​", ""))


def manifests(dumps: Path, folders: list) -> dict:
    res = {}
    for f in folders:
        with open(dumps / f"{f}_manifest.csv", encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                res[str(dumps / f / r["file"])] = r
    return res


def figures(dumps: Path, folders: list, cfg: dict, out: Path) -> dict:
    """Statements about the company. A statement counts if the company is named within 700 characters before it, which covers 'The company reported ...'."""
    key = cfg["company"]["name_regex"]
    pats = {"rr_first": (r"surpassed an? \$([\d.]+) (billion|million) annual revenue run-rate", "Annual revenue run-rate"),
            "rr_last": (r"\$([\d.]+) (billion|million) annualized revenue run-rate in its quarter ended (\w+ \d{4})", "Annualized revenue run-rate, latest quarter"),
            "growth": (r"growing more than (\d+)% year-over-year", "Run-rate growth, year over year"),
            "ai_rr": (r"AI products (?:surpassing|crossing) an? \$([\d.]+) (billion|million)", "AI products run-rate"),
            "valuation": (r"at a \$([\d.]+) (billion|million) valuation", "Latest round valuation"),
            "capacity": (r"\$([\d.]+) (billion|million) of additional debt capacity", "Additional debt capacity"),
            "price": (r"purchase price of \$([\d.]+) per share", "Latest round price per share")}
    man = manifests(dumps, folders)
    hits: dict = collections.defaultdict(list)
    for p, m in man.items():
        path = Path(p)
        if not path.exists() or path.suffix.lower() not in (".htm", ".html"):
            continue
        raw = path.read_bytes()
        if not re.search(rb"revenue run-rate|additional debt capacity|purchase price of \$", raw):
            continue
        t = plain(path)
        for k, (rx, label) in pats.items():
            for x in re.finditer(rx, t, re.I):
                if not re.search(key, t[max(0, x.start() - 700):x.end() + 80], re.I):
                    continue
                unit = {"billion": 1e9, "million": 1e6}.get((x.group(2) if x.re.groups >= 2 else "").lower(), 1)
                v = float(x[1]) * unit if k != "growth" else float(x[1]) / 100
                as_of = ""
                if k == "rr_last":
                    mo, yr = x[3].split()
                    as_of = dt.date(int(yr), MONTHS[mo], calendar.monthrange(int(yr), MONTHS[mo])[1]).isoformat()
                hits[k].append((m["file_date"], m["form"], re.sub(r"\s*\(.*", "", m["filer"]).strip(), label, as_of, v, t[max(0, x.start() - 90):x.end() + 60]))
    marks = sorted(float(r["median_price"]) for r in csv.DictReader(open(out / "data" / "nport_price_monthly.csv", encoding="utf-8")))[-12:]
    if marks:                                  # a stated share price must be near the funds' marks of the past year, which drops other securities' prices
        hits["price"] = [h for h in hits["price"] if marks[0] / 3 <= h[5] <= marks[-1] * 3]
    body = []
    for k in pats:
        if hits[k]:
            # the value most filings state; among those, the latest filing
            common = collections.Counter(h[5] for h in hits[k]).most_common(1)[0][0]
            h = max((h for h in hits[k] if h[5] == common), key=lambda h: h[0])
            body.append([k, h[2], h[1], h[0], h[3], h[4], h[5], len(hits[k]), h[6]])
    return {"company_figures.csv": write(out, "company_figures.csv", ["key", "filer", "form", "filed", "metric", "as_of", "value", "filings", "quote"], body)}


def series_prices(facts_dir: Path, out: Path) -> dict:
    """Cost per share by preferred series, the most common value across fund schedules."""
    prices = collections.defaultdict(collections.Counter)
    for p in facts_dir.glob("*.json"):
        for h in json.load(open(p, encoding="utf-8")).get("holdings", []):
            if h.get("check") != "ok" or not h.get("shares") or not h.get("cost_usd"):
                continue
            m = re.search(r"\bSer(?:ies|\.)?\s+([A-Z])\b", str(h.get("security") or ""))
            if m and h["shares"] > 100:
                prices[m[1]][round(h["cost_usd"] / h["shares"], 2)] += 1
    body = [[k, *v.most_common(1)[0], sum(v.values())] for k, v in sorted(prices.items())]
    return {"series_prices.csv": write(out, "series_prices.csv", ["series", "cost_per_share", "rows_at_this_value", "rows"], body)}


def multiples(dumps: Path, folders: list, cfg: dict, out: Path) -> dict:
    key = cfg["company"]["name_regex"]
    seen, body = set(), []
    for p, m in manifests(dumps, folders).items():
        path = Path(p)
        if not m["form"].startswith("N-CSR") or not path.exists():
            continue
        b_ = path.read_bytes()
        if not re.search(rb"Multiple|EV\s*/", b_) or not re.search(key.encode().replace(b" ?", b"\\s?"), b_, re.I):
            continue
        t = plain(path)
        for x in re.finditer(key + r".{0,130}?(?:Multiple of Revenue|Revenue Multiple|EV ?/ ?revenue[^0-9]{0,30})\s*(\d{1,3}\.\d)x?\s{0,3}[-–]?\s{0,3}(\d{1,3}\.\d)x(?:\s{0,3}\(?(\d{1,3}\.\d)x)?", t, re.I):
            d = re.search(r"(?:period|year|months) ended,? (\w+ \d{1,2}, \d{4})", t, re.I)
            day = dt.datetime.strptime(d[1], "%B %d, %Y").date().isoformat() if d else m["file_date"]
            k = (day, x[1], x[2])
            if k not in seen:
                seen.add(k)
                body.append([day, re.sub(r"\s*\(.*", "", m["filer"]).strip(), m["form"], m["file_date"], x[1], x[2], x[3] or ""])
    body.sort()
    return {"revenue_multiples.csv": write(out, "revenue_multiples.csv", ["valuation_date", "filer", "form", "filed", "low", "high", "central"], body)}


def peer_tables(raw: Path, cfg: dict, out: Path) -> dict:
    """Latest balance-sheet and income items of the listed peers, from XBRL company facts."""
    def dur(f, names):
        for n in names:
            if n in f:
                r = {}
                for x in f[n]["units"].get("USD", []):
                    if "start" in x and x.get("form", "").startswith("10"):
                        r[(dt.date.fromisoformat(x["start"]), dt.date.fromisoformat(x["end"]))] = x["val"]
                if r:
                    return r
        return {}

    def ttm(r, end):
        yr = [v for (a, z), v in r.items() if z == end and 340 <= (z - a).days <= 380]
        if yr:
            return yr[0]
        ytd = sorted(((a, z) for a, z in r if z == end), key=lambda k: k[0])
        if not ytd:
            return None
        a, z = ytd[0]
        fy = [(x, y) for x, y in r if abs((a - y).days) < 5 and 340 <= (y - x).days <= 380]
        pr = [(x, y) for x, y in r if abs((end - y).days - 365) < 8 and abs((a - x).days - 365) < 8]
        return r[fy[0]] + r[(a, z)] - r[pr[0]] if fy and pr else None

    def last(f, names, end):
        for n in names:
            if n in f:
                v = [x["val"] for x in f[n]["units"].get("USD", []) if "start" not in x and x["end"] == end.isoformat()]
                if v:
                    return v[-1]
        return None

    body = []
    for p in cfg["company"]["peers"]:
        all_ = json.load(open(next((raw / "sec_xbrl").glob(f"*_{p}.json")), encoding="utf-8"))["facts"]
        f = all_["us-gaap"]
        rev = dur(f, ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"])
        end = max(k[1] for k in rev)
        tr = ttm(rev, end)
        op = ttm(dur(f, ["OperatingIncomeLoss"]), end)
        da = ttm(dur(f, ["DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet", "DepreciationAndAmortization"]), end)
        sbc = ttm(dur(f, ["ShareBasedCompensation"]), end)
        it = ttm(dur(f, ["InterestExpenseNonoperating", "InterestExpense", "InterestExpenseDebt"]), end)
        cash = last(f, ["CashAndCashEquivalentsAtCarryingValue"], end)
        debt = sum(last(f, [n], end) or 0 for n in ("ConvertibleDebtNoncurrent", "ConvertibleNotesPayableNoncurrent", "ConvertibleLongTermNotesPayable", "LongTermDebtNoncurrent", "ConvertibleDebtCurrent", "ConvertibleNotesPayableCurrent", "LongTermDebtCurrent"))
        e = op + (da or 0) + (sbc or 0) if op is not None else None
        fl = sorted(all_.get("dei", {}).get("EntityPublicFloat", {}).get("units", {}).get("USD", []), key=lambda x: x["end"])
        qr = [v for (a, z), v in rev.items() if z == end and 80 <= (z - a).days <= 100]
        qy = [v for (a, z), v in rev.items() if 355 <= (end - z).days <= 375 and 80 <= (z - a).days <= 100]
        q4, qg = (qr[0] * 4, round(qr[0] / qy[0] - 1, 4)) if qr and qy else ("", "")
        fv, fd, fq, rf, cf, df, mf, ev = (fl[-1]["val"] if fl else None), (fl[-1]["end"] if fl else ""), "", None, None, None, "", ""
        if fv:                                 # the quarter end nearest the float date, and the twelve months to it
            qs = sorted({k[1] for k in rev}, key=lambda d: abs((d - dt.date.fromisoformat(fd)).days))
            fq = qs[0]
            rf = ttm(rev, fq)
            cf = (last(f, ["CashAndCashEquivalentsAtCarryingValue"], fq) or 0) + (last(f, ["ShortTermInvestments", "MarketableSecuritiesCurrent", "AvailableForSaleSecuritiesDebtSecuritiesCurrent"], fq) or 0)
            df = sum(last(f, [n], fq) or 0 for n in ("ConvertibleDebtNoncurrent", "ConvertibleNotesPayableNoncurrent", "ConvertibleLongTermNotesPayable", "LongTermDebtNoncurrent", "ConvertibleDebtCurrent", "ConvertibleNotesPayableCurrent", "LongTermDebtCurrent"))
            if rf:
                mf, ev = round(fv / rf, 2), round((fv - cf + df) / rf, 2)
        body.append([p, end, tr, op, da, sbc, e, it or "", cash or "", debt or 0, round((debt or 0) / e, 2) if e and e > 0 else "", round(e / it, 1) if e and it else "", fv or "", fd, fq, rf or "", cf or "", df or "", mf, ev, q4, qg])
    return {"peer_credit_xbrl.csv": write(out, "peer_credit_xbrl.csv", ["company", "ttm_end", "ttm_revenue_usd", "ttm_operating_income_usd", "ttm_d_and_a_usd", "ttm_sbc_usd", "ttm_ebitda_ex_sbc_usd", "ttm_interest_expense_usd",
                                                                         "cash_usd", "debt_usd", "debt_to_ebitda", "ebitda_to_interest", "public_float_usd", "public_float_date", "float_quarter_end", "ttm_rev_at_float_usd", "cash_and_st_inv_at_float_usd", "debt_at_float_usd", "float_to_ttm_rev", "float_ev_to_ttm_rev", "latest_quarter_x4_usd", "latest_quarter_yoy"], body)}
