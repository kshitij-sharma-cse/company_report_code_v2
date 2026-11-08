#!/usr/bin/env python3
"""Step 0: download every filing that EDGAR full-text search finds for the company's name and aliases (all forms, 2001 on).

    export SEC_USER_AGENT="your-org contact@your-domain"
    python tools/download_filings.py databricks                     # terms: config "name" + company.aliases
    python tools/download_filings.py databricks --term "Databricks Inc" --skip-forms NPORT-P,NPORT-P/A --dumps DIR

Terms come from model_config/<co>.json ("name" and company.aliases), plus any --term. Search ignores punctuation and case,
so "Heal, Inc." and "Heal Inc" are one query. N-PORT is skipped by default: nport.py reads it as structured XML.
Writes
    <dumps>/<co>/<form>/<accession>_<file>        one file per hit (the hit is a document: exhibits come as their own hit)
    <dumps>/<co>_manifest.csv                      file,form,file_date,accession,filer,sec_url,index_url,status
    <dumps>/<co>_fts_hits.csv                      every hit with the term(s) that found it, skipped forms included
Resumable: files already on disk are kept, failed ones are retried on the next run. At most 5 requests/s.
With --only-terms the new rows are merged into the existing manifest and hit list (keyed by file), so a run for one
extra phrase does not drop the filings found before.
"""
import argparse
import csv
import datetime as dt
import io
import json
import re
import urllib.parse
from pathlib import Path

import sec_http as sec

EFTS = "https://efts.sec.gov/LATEST/search-index"
CAP = 10000                                          # EFTS returns at most 10,000 hits per query; longer ranges are split


def search(term: str, s: dt.date, e: dt.date, hits: dict) -> int:
    """All hits of one phrase in [s, e]; a range over the cap is halved until each part fits."""
    q = {"q": f'"{term}"', "dateRange": "custom", "startdt": s.isoformat(), "enddt": e.isoformat()}
    first = sec.get(f"{EFTS}?{urllib.parse.urlencode(q)}").json()["hits"]
    total = first["total"]["value"]
    if (total >= CAP or first["total"].get("relation") == "gte") and s < e:
        mid = s + (e - s) / 2
        return search(term, s, mid, hits) + search(term, mid + dt.timedelta(days=1), e, hits)
    if total >= CAP:
        print(f"  {term!r} {s}: over {CAP} hits in one day, list is cut", flush=True)
    n, page = 0, first["hits"]
    while page:
        for h in page:
            src = h["_source"]
            x = hits.setdefault(h["_id"], {"id": h["_id"], "file": h["_id"].split(":", 1)[1], "form": src.get("form") or (src.get("root_forms") or ["?"])[0],
                                           "file_date": src.get("file_date", ""), "accession": src.get("adsh") or h["_id"].split(":", 1)[0],
                                           "cik": (src.get("ciks") or ["0"])[0], "filer": (src.get("display_names") or [""])[0], "terms": []})
            if term not in x["terms"]:
                x["terms"].append(term)
        n += len(page)
        if n >= min(total, CAP):
            break
        page = sec.get(f"{EFTS}?{urllib.parse.urlencode({**q, 'from': n})}").json()["hits"]["hits"]
    return n


def write_csv(p: Path, head: list, rows: list, key, merge: bool, order) -> int:
    """Atomic write; with merge, rows already in the file stay unless a new row has the same key (then sorted by date,
    accession, file, as a full run writes them). Returns rows written."""
    if merge and p.exists():
        new = {key(r) for r in rows}
        old = [r for r in csv.reader(open(p, encoding="utf-8")) if r][1:]
        rows = sorted([r for r in old if key(r) not in new] + rows, key=order)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(head)
    w.writerows(rows)
    sec.write_atomic(p, buf.getvalue().encode("utf-8"))
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("company", help="config / folder name, e.g. databricks")
    ap.add_argument("--config", default="", help="default: coref_experiment/company_report/model_config/<company>.json")
    ap.add_argument("--term", action="append", default=[], help="extra search phrase (repeatable)")
    ap.add_argument("--only-terms", action="store_true", help="search only the --term phrases, not the config name and aliases")
    ap.add_argument("--skip-forms", default="NPORT-P,NPORT-P/A", help="comma list of forms listed but not downloaded ('' = none)")
    ap.add_argument("--dumps", default=str(sec.ROOT / "dumps"))
    ap.add_argument("--start", default="2001-01-01", help="first filing date searched (full-text search starts in 2001)")
    ap.add_argument("--list-only", action="store_true", help="write the hit list, download nothing")
    a = ap.parse_args()
    sec.user_agent()
    terms = list(a.term)
    if not a.only_terms:
        cfg = sec.load_config(a.company, a.config)
        terms = [cfg.get("name", "")] + list(cfg.get("company", {}).get("aliases", [])) + terms
    key = lambda t: " ".join(re.findall(r"\w+", t.lower()))   # noqa: E731  the search's own normalisation
    uniq = {}
    for t in terms:
        if key(t) and key(t) not in uniq:
            uniq[key(t)] = t.strip()
    if not uniq:
        raise SystemExit("no search terms: set \"name\" / company.aliases in the config or pass --term")
    skip = {f.strip().upper() for f in a.skip_forms.split(",") if f.strip()}
    dumps = Path(a.dumps)
    dest = dumps / a.company
    dest.mkdir(parents=True, exist_ok=True)
    hits: dict = {}
    for t in uniq.values():
        n = search(t, dt.date.fromisoformat(a.start), dt.date.today(), hits)
        print(f"{t!r}: {n} hits, {len(hits)} distinct so far", flush=True)
    order = sorted(hits.values(), key=lambda x: (x["file_date"], x["id"]))
    merge = a.only_terms                             # a partial search adds to the lists, it does not replace them
    hits_rows = [[x["accession"], x["file"], x["form"], x["file_date"], x["filer"], "|".join(x["terms"])] for x in order]
    if merge and (dumps / f"{a.company}_fts_hits.csv").exists():   # a hit found again keeps the terms that found it before
        old = {(r[0], r[1]): r[5] for r in list(csv.reader(open(dumps / f"{a.company}_fts_hits.csv", encoding="utf-8")))[1:] if len(r) > 5}
        for r in hits_rows:
            r[5] = "|".join(dict.fromkeys([t for t in old.get((r[0], r[1]), "").split("|") if t] + r[5].split("|")))
    n_hits = write_csv(dumps / f"{a.company}_fts_hits.csv", ["accession", "file", "form", "file_date", "filer", "terms"], hits_rows,
                       lambda r: (r[0], r[1]), merge, lambda r: (r[3], r[0], r[1]))
    if a.list_only:
        print(json.dumps({"hits": len(hits), "terms": list(uniq.values()), "hit_list_rows": n_hits}))
        return
    rows, got = [], 0
    for x in order:
        if x["form"].upper() in skip:
            continue
        acc, base = x["accession"], f"https://www.sec.gov/Archives/edgar/data/{int(x['cik'])}/{x['accession'].replace('-', '')}"
        rel = f"{x['form'].replace('/', '-')}/{acc}_{x['file']}"
        path, status = dest / rel, "ok"
        if not path.exists():
            r = sec.get(f"{base}/{x['file']}")
            if r is not None and r.status_code == 200 and r.content:
                sec.write_atomic(path, r.content)
                got += 1
                if got % 25 == 0:
                    print(f"  downloaded {got}", flush=True)
            else:
                status = f"HTTP {getattr(r, 'status_code', 'none')}"
        rows.append([rel, x["form"], x["file_date"], acc, x["filer"], f"{base}/{x['file']}", f"{base}/{acc}-index.html", status])
    n_man = write_csv(dumps / f"{a.company}_manifest.csv", ["file", "form", "file_date", "accession", "filer", "sec_url", "index_url", "status"],
                      rows, lambda r: r[0], merge, lambda r: (r[2], r[3], r[0].split("_", 1)[-1]))
    print(json.dumps({"terms": list(uniq.values()), "hits": len(hits), "in_manifest": n_man, "from_this_search": len(rows), "downloaded_now": got,
                      "ok": sum(r[-1] == "ok" for r in rows), "failed": sum(r[-1] != "ok" for r in rows),
                      "skipped": sum(x["form"].upper() in skip for x in order), "skip_forms": sorted(skip)}))


if __name__ == "__main__":
    main()
