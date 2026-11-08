#!/usr/bin/env python3
"""Form N-PORT holdings of one company. N-PORT is structured XML, so no model is used and every value is the filed value.

  nport.py list     COMPANY_TERM --dump DIR     EDGAR full-text search -> DIR/manifest.csv
  nport.py download --dump DIR                  fetches each primary_doc.xml (gzipped), resumes, ~5 requests/s
  nport.py parse    COMPANY_TERM --dump DIR --out FILE.csv     one row per position that names the company
  nport.py build    COMPANY_TERM --dump DIR --out FILE.csv     merged, amendments applied, classified (input to build_report --nport-csv)
"""
import argparse, collections, csv, datetime as dt, gzip, json, re, sys, threading, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

UA = {"User-Agent": "projectx-filings-research testable485@gmail.com"}
FORMS = ["NPORT-P", "NPORT-P/A"]
_lock, _next = threading.Lock(), [0.0]


def get(url: str, tries: int = 6) -> bytes:
    for i in range(tries):
        with _lock:                                     # SEC asks for at most 10 requests/s; stay at 5
            wait = _next[0] - time.time()
            _next[0] = max(_next[0], time.time()) + 0.2
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
                return r.read()
        except Exception as e:
            if i == tries - 1:
                raise
            time.sleep(2 ** i + (30 if "429" in str(e) or "403" in str(e) else 0))


def windows():
    d = dt.date(2019, 1, 1)
    today = dt.date.today()
    while d <= today:
        e = min(d + dt.timedelta(days=30), today)
        yield d, e
        d = e + dt.timedelta(days=1)


def cmd_list(a):
    dump = Path(a.dump); dump.mkdir(parents=True, exist_ok=True)
    seen = {}
    for form in FORMS:
        for s, e in windows():
            frm, total = 0, 1
            while frm < total and frm < 10000:
                q = urllib.parse.urlencode({"q": f'"{a.term}"', "forms": form, "startdt": s, "enddt": e, "from": frm}, safe="/")
                h = json.loads(get(f"https://efts.sec.gov/LATEST/search-index?{q}"))["hits"]
                total = h["total"]["value"]
                for x in h["hits"]:
                    adsh, doc = x["_id"].split(":", 1)
                    src = x["_source"]
                    e0 = seen.setdefault(adsh, {"accession": adsh, "document": "primary_doc.xml", "form": src.get("form"),
                                                "file_date": src.get("file_date"), "period": src.get("period_ending"),
                                                "cik": str(int(src["ciks"][0])), "filer": (src.get("display_names") or [""])[0],
                                                "hit_in_primary": ""})
                    if doc == "primary_doc.xml":          # a trust-wide exhibit can name a holding of another series
                        e0["hit_in_primary"] = "Y"
                if not h["hits"]:
                    break
                frm += len(h["hits"])
            if total >= 10000:
                print("window over 10,000 hits, list is cut:", s, e)
            print(form, s, e, "window", total, "total so far", len(seen), flush=True)
    rows = sorted(seen.values(), key=lambda r: r["file_date"])
    with open(dump / "manifest.csv", "w", newline="") as f:
        w = csv.DictWriter(f, list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    print(len(rows), "filings")


def sec_url(r): return f"https://www.sec.gov/Archives/edgar/data/{r['cik']}/{r['accession'].replace('-', '')}/{r['document']}"


def cmd_download(a):
    dump = Path(a.dump); xml = dump / "xml"; xml.mkdir(exist_ok=True)
    rows = list(csv.DictReader(open(dump / "manifest.csv")))
    if a.limit:
        rows = rows[:a.limit]
    bad = []
    done = [0]

    def one(r):
        p = xml / f"{r['accession']}.xml.gz"
        if not p.exists():
            try:
                data = get(sec_url(r))
                ET.fromstring(data)                     # a block page or truncated body must not be cached
                tmp = p.with_suffix(".tmp"); tmp.write_bytes(gzip.compress(data, 6)); tmp.rename(p)
            except Exception as e:
                bad.append((r["accession"], str(e)[:100]))
        done[0] += 1
        if done[0] % 200 == 0:
            print(done[0], "of", len(rows), "failed", len(bad), flush=True)

    with ThreadPoolExecutor(6) as pool:
        list(pool.map(one, rows))
    (dump / "download_failed.json").write_text(json.dumps(bad, indent=1))
    print("done", len(rows), "failed", len(bad))


FIELDS = ["accession", "form", "file_date", "registrant", "reg_cik", "series", "series_id", "fiscal_year_end", "period", "net_assets",
          "name", "title", "lei", "cusip", "balance", "units", "currency", "value_usd", "pct_net_assets", "payoff", "asset_cat",
          "issuer_cat", "country", "restricted", "fair_value_level", "is_derivative", "matched_in", "sec_url"]


def parse_one(r, xml: Path, pat):
    root = ET.fromstring(gzip.open(xml / f"{r['accession']}.xml.gz").read())
    tag = lambda e: e.tag.split("}")[-1]   # noqa: E731
    g = {}
    for e in root.iter():
        t = tag(e)
        if t in ("regName", "regCik", "seriesName", "seriesId", "repPdEnd", "repPdDate", "netAssets") and t not in g:
            g[t] = (e.text or "").strip()
    out = []
    for inv in root.iter():
        if tag(inv) != "invstOrSec":
            continue
        kids = {tag(x): (x.text or "").strip() for x in inv}
        name, title = kids.get("name", ""), kids.get("title", "")
        matched = "name/title" if pat.search(f"{name} {title}") else ""
        deriv = any(tag(x) == "derivativeInfo" for x in inv)
        if not matched:
            blob = " ".join(f"{v} {(x.text or '')}" for x in inv.iter() for v in x.attrib.values())
            if pat.search(blob):
                matched = "other field"
        if not matched:
            continue
        cat = kids.get("assetCat", "")
        if not cat:
            for x in inv:
                if tag(x) == "assetConditional":
                    cat = x.get("assetCat", "") + ":" + x.get("desc", "")
        out.append({"accession": r["accession"], "form": r["form"], "file_date": r["file_date"], "registrant": g.get("regName", ""),
                    "reg_cik": g.get("regCik", ""), "series": g.get("seriesName", ""), "series_id": g.get("seriesId", ""),
                    "fiscal_year_end": g.get("repPdEnd", ""), "period": g.get("repPdDate", ""), "net_assets": g.get("netAssets", ""),
                    "name": name, "title": title, "lei": kids.get("lei", ""), "cusip": kids.get("cusip", ""), "balance": kids.get("balance", ""),
                    "units": kids.get("units", ""), "currency": kids.get("curCd", ""), "value_usd": kids.get("valUSD", ""),
                    "pct_net_assets": kids.get("pctVal", ""), "payoff": kids.get("payoffProfile", ""), "asset_cat": cat,
                    "issuer_cat": kids.get("issuerCat", ""), "country": kids.get("invCountry", ""), "restricted": kids.get("isRestrictedSec", ""),
                    "fair_value_level": kids.get("fairValLevel", ""), "is_derivative": "Y" if deriv else "", "matched_in": matched,
                    "sec_url": sec_url(r)})
    return out


def cmd_parse(a):
    dump = Path(a.dump); xml = dump / "xml"
    pat = re.compile(re.escape(a.term), re.I)
    rows, none, missing = [], 0, 0
    for r in csv.DictReader(open(dump / "manifest.csv")):
        if not (xml / f"{r['accession']}.xml.gz").exists():
            missing += 1
            continue
        got = parse_one(r, xml, pat)
        none += not got
        rows += got
    with open(a.out, "w", newline="") as f:
        w = csv.DictWriter(f, FIELDS); w.writeheader(); w.writerows(rows)
    print(len(rows), "rows;", none, "filings with no matching position;", missing, "not downloaded")


SERIES = re.compile(r"\bSER(?:IES)?\.?\s*([A-Z])(?:-\d)?\b", re.I)
OUT = ["report_date", "filing_date", "form", "accession", "fund_house", "registrant", "series", "series_id", "cik", "name", "title",
       "position_type", "share_class", "shares", "par_or_units", "unit", "value_usd", "filed_price_per_share", "pct_net_assets",
       "fund_net_assets", "asset_cat", "fair_value_level", "restricted", "source", "sec_url"]


def position(name: str, title: str, cat: str, unit: str, term: str) -> str:
    text = f"{name} {title}".upper()
    t = term.upper()
    if cat == "LON" or unit == "PA":
        return "funded_loan"
    if not (name.upper().startswith(t) or (name in ("", "N/A") and title.upper().startswith(t))):
        return "spv_interest"                            # a vehicle that holds the company, its value is not a share price
    return "equity" if unit == "NS" and cat in ("EP", "EC") and "WARRANT" not in text else "other"


def share_class(name: str, title: str, cat: str) -> str:
    m = SERIES.search(f"{name} {title}")
    if m:
        return "Series " + m.group(1).upper()
    return "Common" if cat == "EC" else "Preferred, series not stated"


def cmd_build(a):
    """Team database rows first; XML rows only for filings the database does not have. A later filing of the same fund and
    date (an amendment) replaces the earlier one."""
    dump = Path(a.dump)
    rows = []
    houses = {}
    for r in csv.DictReader(open(dump / "duckdb_rows.csv")):
        houses[r["series_id"]] = r["fund_house"]
        cik = str(int(r["cik"]))
        rows.append(dict(report_date=r["report_date"], filing_date=r["filing_date"], form=r["sub_type"], accession=r["ACCESSION_NUMBER"],
                         fund_house=r["fund_house"], registrant=r["registrant_name"], series=r["series_name"], series_id=r["series_id"], cik=cik,
                         name=r["ISSUER_NAME"], title=r["ISSUER_TITLE"], balance=r["BALANCE"], unit=r["UNIT"], value_usd=r["CURRENCY_VALUE"],
                         pct_net_assets=r["PERCENTAGE"], fund_net_assets=r["fund_net_assets"], asset_cat=r["ASSET_CAT"],
                         fair_value_level=r["FAIR_VALUE_LEVEL"], restricted=r["IS_RESTRICTED_SECURITY"], source="team database"))
    have = {r["accession"] for r in rows}
    for r in csv.DictReader(open(dump / "nport_rows.csv")):
        if r["accession"] in have:
            continue
        rows.append(dict(report_date=r["period"], filing_date=r["file_date"], form=r["form"], accession=r["accession"],
                         fund_house=houses.get(r["series_id"], r["registrant"]), registrant=r["registrant"], series=r["series"] or r["registrant"],
                         series_id=r["series_id"], cik=str(int(r["reg_cik"])), name=r["name"], title=r["title"], balance=r["balance"], unit=r["units"],
                         value_usd=r["value_usd"], pct_net_assets=r["pct_net_assets"], fund_net_assets=r["net_assets"], asset_cat=r["asset_cat"].split(":")[0],
                         fair_value_level=r["fair_value_level"], restricted=r["restricted"], source="EDGAR XML"))
    latest = {}
    for r in rows:
        k = (r["series_id"] or r["cik"], r["report_date"])
        latest[k] = max(latest.get(k, ""), r["filing_date"] + r["accession"])
    kept, dropped = [], 0
    for r in rows:
        if r["series"] in ("", "N/A"):
            r["series"] = r["registrant"]
        if latest[(r["series_id"] or r["cik"], r["report_date"])] != r["filing_date"] + r["accession"]:
            dropped += 1
            continue
        bal, val = float(r["balance"] or 0), float(r["value_usd"] or 0)
        pos = position(r["name"], r["title"], r["asset_cat"], r["unit"], a.term)
        r.update(position_type=pos, share_class=share_class(r["name"], r["title"], r["asset_cat"]) if pos == "equity" else "",
                 shares=r["balance"] if pos == "equity" else "", par_or_units="" if pos == "equity" else r["balance"],
                 filed_price_per_share=round(val / bal, 4) if pos == "equity" and bal else "",
                 sec_url=f"https://www.sec.gov/Archives/edgar/data/{r['cik']}/{r['accession'].replace('-', '')}/primary_doc.xml")
        kept.append(r)
    kept.sort(key=lambda r: (r["report_date"], r["series"], r["title"]))
    with open(a.out, "w", newline="") as f:
        w = csv.DictWriter(f, OUT, extrasaction="ignore"); w.writeheader(); w.writerows(kept)
    print(len(rows), "rows,", dropped, "replaced by an amendment,", len(kept), "kept:", collections.Counter(r["position_type"] for r in kept).most_common())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["list", "download", "parse", "build"])
    ap.add_argument("term", nargs="?", default="Databricks")
    ap.add_argument("--dump", required=True)
    ap.add_argument("--out", default="nport.csv")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    {"list": cmd_list, "download": cmd_download, "parse": cmd_parse, "build": cmd_build}[a.cmd](a)
