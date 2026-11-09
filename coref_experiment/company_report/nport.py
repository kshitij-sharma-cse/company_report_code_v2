#!/usr/bin/env python3
"""Form N-PORT holdings of one company. N-PORT is structured XML, so no model is used and every value is the filed value.

  nport.py list     [COMPANY_TERM] --dump DIR [--term T ...]   EDGAR full-text search -> DIR/manifest.csv
  nport.py download --dump DIR                  fetches each primary_doc.xml (gzipped), resumes, ~5 requests/s
  nport.py parse    [COMPANY_TERM] --dump DIR --out FILE.csv     one row per position that names the company
  nport.py export-db [COMPANY_TERM] --dump DIR --db nport_team.duckdb [--config model_config/<co>.json]
                                                team database rows whose issuer name matches -> DIR/duckdb_rows.csv
  nport.py build    [COMPANY_TERM] --dump DIR --out FILE.csv     merged, amendments applied, classified (input to build_report --nport-csv)
The config is --config, else model_config/<co>.json with <co> from a DIR named <co>_nport or from the term's words.
list searches every name form: the term, the config "name" and company.aliases, and each --term (a filing that prints only
"Acme Corporation" is missed by a search for "Acme Corp"); hits are merged by accession and manifest column search_terms says
which forms found each filing. The name pattern of parse, export-db and build is the config company.name_regex, else the
words of the term and the config "name" (never the aliases); COMPANY_TERM is optional when the config is found.
SEC requests need a contact: export SEC_USER_AGENT="your-org contact@your-domain".
"""
import argparse, collections, csv, datetime as dt, gzip, json, os, re, ssl, sys, threading, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

UA = {}
CONFIG = Path(__file__).resolve().parent / "model_config"
FORMS = ["NPORT-P", "NPORT-P/A"]
_lock, _next = threading.Lock(), [0.0]


def sec_setup():
    """SEC asks every client to name itself; urllib verifies TLS with certifi's roots (the system store fails on some Macs)."""
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua:
        sys.exit('SEC_USER_AGENT is not set. SEC requires a contact in every request, e.g.\n  export SEC_USER_AGENT="your-org contact@your-domain"')
    UA["User-Agent"] = ua
    try:
        import certifi
        UA["_ctx"] = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        UA["_ctx"] = ssl.create_default_context()


def get(url: str, tries: int = 6) -> bytes:
    for i in range(tries):
        with _lock:                                     # SEC asks for at most 10 requests/s; stay at 5
            wait = _next[0] - time.time()
            _next[0] = max(_next[0], time.time()) + 0.2
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]}), timeout=60, context=UA["_ctx"]) as r:
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
    terms = name_forms(a)
    if not terms:
        sys.exit("list: no search term; give COMPANY_TERM, --term or a --config with name / company.aliases")
    print("search terms:", terms, file=sys.stderr)
    sec_setup()
    dump = Path(a.dump); dump.mkdir(parents=True, exist_ok=True)
    seen, failed = {}, []
    for term, form in ((t, f) for t in terms for f in FORMS):
        before = len(seen)
        for s, e in windows():
            frm, total = 0, 1
            while frm < total and frm < 10000:
                q = urllib.parse.urlencode({"q": f'"{term}"', "forms": form, "startdt": s, "enddt": e, "from": frm}, safe="/")
                for i in range(4):                     # EFTS sometimes answers with an error body instead of hits
                    body = json.loads(get(f"https://efts.sec.gov/LATEST/search-index?{q}"))
                    if "hits" in body:
                        break
                    time.sleep(5 * (i + 1))
                else:
                    failed.append({"term": term, "form": form, "start": str(s), "end": str(e), "from": frm, "answer": str(body)[:200]})
                    break
                h = body["hits"]
                total = h["total"]["value"]
                for x in h["hits"]:
                    adsh, doc = x["_id"].split(":", 1)
                    src = x["_source"]
                    e0 = seen.setdefault(adsh, {"accession": adsh, "document": "primary_doc.xml", "form": src.get("form"),
                                                "file_date": src.get("file_date"), "period": src.get("period_ending"),
                                                "cik": str(int(src["ciks"][0])), "filer": (src.get("display_names") or [""])[0],
                                                "hit_in_primary": "", "search_terms": ""})
                    if term not in e0["search_terms"].split(" | "):
                        e0["search_terms"] = " | ".join(x for x in (e0["search_terms"], term) if x)
                    if doc == "primary_doc.xml":          # a trust-wide exhibit can name a holding of another series
                        e0["hit_in_primary"] = "Y"
                if not h["hits"]:
                    break
                frm += len(h["hits"])
            if total >= 10000:
                print("window over 10,000 hits, list is cut:", term, form, s, e)
        print(f"{term!r} {form}: {len(seen) - before} new filings, {len(seen)} in all", flush=True)
    rows = sorted(seen.values(), key=lambda r: r["file_date"])
    with open(dump / "manifest.csv", "w", newline="") as f:      # written even with 0 filings: a company no fund holds is a result
        w = csv.DictWriter(f, LIST_COLS); w.writeheader(); w.writerows(rows)
    (dump / "list_failed.json").write_text(json.dumps(failed, indent=1))
    print(len(rows), "filings" + ("" if rows else f" for {terms} (no N-PORT filing names the company)"))
    if failed:
        sys.exit(f"list: {len(failed)} search window(s) failed after retries, the list may be short; see {dump / 'list_failed.json'}")


LIST_COLS = ["accession", "document", "form", "file_date", "period", "cik", "filer", "hit_in_primary", "search_terms"]


def sec_url(r): return f"https://www.sec.gov/Archives/edgar/data/{r['cik']}/{r['accession'].replace('-', '')}/{r['document']}"


def cmd_download(a):
    sec_setup()
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
    pat = re.compile(edged(name_regex(a)))
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


def position(name: str, title: str, cat: str, unit: str, issuer) -> str:
    """issuer: compiled pattern that matches a name starting with the company's own name (not a vehicle named after it)."""
    text = f"{name} {title}".upper()
    if cat == "LON" or unit == "PA":
        return "funded_loan"
    if not (issuer.match(name) or (name in ("", "N/A") and issuer.match(title))):
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
    db_rows, xml_rows = dump / "duckdb_rows.csv", dump / "nport_rows.csv"   # export-db output / parse output; either may be absent
    if not db_rows.exists() and not xml_rows.exists():
        sys.exit(f"build: neither {db_rows} (export-db) nor {xml_rows} (parse) exists")
    issuer = re.compile(rf"(?i)\W*(?:{name_regex(a)})(?:\W|$)")
    rows = []
    houses = {}
    for r in (csv.DictReader(open(db_rows)) if db_rows.exists() else []):
        houses[r["series_id"]] = r["fund_house"]
        cik = str(int(r["cik"]))
        rows.append(dict(report_date=r["report_date"], filing_date=r["filing_date"], form=r["sub_type"], accession=r["ACCESSION_NUMBER"],
                         fund_house=r["fund_house"], registrant=r["registrant_name"], series=r["series_name"], series_id=r["series_id"], cik=cik,
                         name=r["ISSUER_NAME"], title=r["ISSUER_TITLE"], balance=r["BALANCE"], unit=r["UNIT"], value_usd=r["CURRENCY_VALUE"],
                         pct_net_assets=r["PERCENTAGE"], fund_net_assets=r["fund_net_assets"], asset_cat=r["ASSET_CAT"],
                         fair_value_level=r["FAIR_VALUE_LEVEL"], restricted=r["IS_RESTRICTED_SECURITY"], source="team database"))
    have = {r["accession"] for r in rows}
    for r in (csv.DictReader(open(xml_rows)) if xml_rows.exists() else []):
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
        pos = position(r["name"], r["title"], r["asset_cat"], r["unit"], issuer)
        r.update(position_type=pos, share_class=share_class(r["name"], r["title"], r["asset_cat"]) if pos == "equity" else "",
                 shares=r["balance"] if pos == "equity" else "", par_or_units="" if pos == "equity" else r["balance"],
                 filed_price_per_share=round(val / bal, 4) if pos == "equity" and bal else "",
                 sec_url=f"https://www.sec.gov/Archives/edgar/data/{r['cik']}/{r['accession'].replace('-', '')}/primary_doc.xml")
        kept.append(r)
    kept.sort(key=lambda r: (r["report_date"], r["series"], r["title"]))
    with open(a.out, "w", newline="") as f:
        w = csv.DictWriter(f, OUT, extrasaction="ignore"); w.writeheader(); w.writerows(kept)
    print(len(rows), "rows,", dropped, "replaced by an amendment,", len(kept), "kept:", collections.Counter(r["position_type"] for r in kept).most_common())


DB_COLS = ["series_id", "fund_house", "cik", "report_date", "filing_date", "sub_type", "ACCESSION_NUMBER", "registrant_name", "series_name",
           "ISSUER_NAME", "ISSUER_TITLE", "BALANCE", "UNIT", "CURRENCY_VALUE", "PERCENTAGE", "fund_net_assets", "ASSET_CAT",
           "FAIR_VALUE_LEVEL", "IS_RESTRICTED_SECURITY"]


def config_path(a) -> Path | None:
    """--config, else model_config/<co>.json with <co> from a dump folder named <co>_nport or from the term's words."""
    if a.config:
        if not Path(a.config).exists():
            sys.exit(f"--config: {a.config} not found")
        return Path(a.config)
    cands = [re.sub(r"_nport$", "", Path(a.dump).resolve().name)] + (["_".join(re.findall(r"\w+", a.term.lower()))] if a.term else [])
    return next((CONFIG / f"{c}.json" for c in cands if (CONFIG / f"{c}.json").exists()), None)


def load_config(a) -> dict:
    p = config_path(a)
    return json.loads(p.read_text(encoding="utf-8")) if p else {}


def name_forms(a) -> list[str]:
    """Every name form to search: the term, the config "name" and company.aliases, then each --term; case-insensitive
    duplicates once."""
    cfg = load_config(a)
    forms = [a.term or "", cfg.get("name", ""), *cfg.get("company", {}).get("aliases", []), *(a.terms or [])]
    out = {}
    for f in forms:
        f = re.sub(r"\s+", " ", str(f or "")).strip()
        if f and f.lower() not in out:
            out[f.lower()] = f
    return list(out.values())


def name_regex(a) -> str:
    """Company name pattern: config company.name_regex, else the term and the config "name" (not the aliases: a generic
    alias such as "Heal, Inc." would let other issuers in as the company's own equity), each one's words with any
    punctuation or space between them ("Get Heal, Inc." -> Get, Heal, Inc joined by \\W+, which also matches
    'GET HEAL INC'), as alternatives."""
    p = config_path(a)
    cfg = load_config(a)
    rx = cfg.get("company", {}).get("name_regex", "")
    forms = list(dict.fromkeys(f.strip() for f in (a.term or "", cfg.get("name", "")) if f and f.strip()))
    if not rx and not forms:
        sys.exit("no company name: give COMPANY_TERM or a --config with company.name_regex or name")
    if not getattr(a, "_said", False):
        print("name pattern from", f"{p} company.name_regex" if rx else f"{forms} (no config name_regex; aliases are not used)", file=sys.stderr)
        a._said = True
    return rx or "|".join(r"\W+".join(re.escape(w) for w in re.findall(r"\w+", f)) for f in forms if re.search(r"\w", f))


def edged(rx: str) -> str:
    """Whole-word match that also works after punctuation ('Inc.') and in DuckDB's RE2 (a word boundary fails after a non-word character at the end)."""
    return rf"(?i)(?:^|\W)(?:{rx})(?:\W|$)"


def cmd_export_db(a):
    """Team database -> DIR/duckdb_rows.csv, the input `build` reads first. Matches the issuer name (and title with --match-title)."""
    import duckdb
    if not a.db or not Path(a.db).exists():
        sys.exit(f"--db: N-PORT team database not found: {a.db!r}")
    dump = Path(a.dump); dump.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(a.db, read_only=True, config={"enable_external_access": False, "autoload_known_extensions": False, "autoinstall_known_extensions": False})
    pat = edged(name_regex(a))
    where = "regexp_matches(ISSUER_NAME, ?)" + (" OR regexp_matches(coalesce(ISSUER_TITLE, ''), ?)" if a.match_title else "")
    try:
        rows = con.execute(f"SELECT {', '.join(DB_COLS)} FROM holdings WHERE {where} ORDER BY report_date, series_id, ACCESSION_NUMBER",
                           [pat] * (2 if a.match_title else 1)).fetchall()
    except duckdb.InvalidInputException as e:      # DuckDB uses RE2: no lookahead, lookbehind or backreference
        sys.exit(f"export-db: pattern {pat!r} is not RE2-compatible; company.name_regex must avoid lookarounds and backreferences ({e})")
    with open(dump / "duckdb_rows.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(DB_COLS); w.writerows(rows)
    if not rows:                                   # written anyway (header only) so `build` runs, but said loudly
        print(f"export-db: 0 holdings whose issuer name matches {pat!r}: no fund in the team database holds the company "
              "under this name (if that is unexpected, check company.name_regex)")
        return
    print(len(rows), "rows; pattern", pat, "; issuer names:", collections.Counter(r[9] for r in rows).most_common(12))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["list", "download", "parse", "build", "export-db"])
    ap.add_argument("term", nargs="?", default="", help="a company name form; optional when the config is found (--config or DIR <co>_nport)")
    ap.add_argument("--term", dest="terms", action="append", default=[], help="list: one more name form to search (repeatable)")
    ap.add_argument("--dump", required=True)
    ap.add_argument("--out", default="nport.csv")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--db", default="", help="export-db: the team N-PORT database (table holdings)")
    ap.add_argument("--config", default="", help="model_config/<co>.json; its company.name_regex is the name pattern (default: found from DIR or the term)")
    ap.add_argument("--match-title", action="store_true", help="export-db: also match ISSUER_TITLE (an SPV named after the company)")
    a = ap.parse_args()
    {"list": cmd_list, "download": cmd_download, "parse": cmd_parse, "build": cmd_build, "export-db": cmd_export_db}[a.cmd](a)
