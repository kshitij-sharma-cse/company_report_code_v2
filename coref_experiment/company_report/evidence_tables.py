#!/usr/bin/env python3
"""Read every number by rule, with no model: iXBRL tags, then HTML tables by their printed column headers, then fixed text layouts.
One row per source cell group. Every row keeps the filing, table, row, printed header, printed unit and raw cells. A row that cannot be tied to a column and a unit goes to review, never into evidence.

    python evidence_tables.py databricks --folders databricks databricks_ncsr databricks_delta
"""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import html
import json
import multiprocessing as mp
import re
from pathlib import Path

csv.field_size_limit(10**8)
HERE = Path(__file__).parent
MONTH = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
COL = [("commitment", r"unfunded|commitment|undrawn"), ("value", r"\b(fair|market)?\s*value\b"), ("cost", r"\bcost\b"), ("qty", r"\bshares\b|\bprincipal\b|\bpar\b|\bunits\b|\bquantity\b|\bface\b|\bnumber of\b"), ("date", r"\bdate\b|\bacquired\b"), ("pct", r"%|percent")]
HEAD_UNIT = re.compile(r"\(?\$?\s*000[’'`]?s?\)?|thousands|millions|\$\s?m\b", re.I)
PAGE_UNIT = re.compile(r"(?:\bin\s|\$\s|expressed in\s|amounts?\s|value\s|cost\s)[^.<]{0,40}?(thousands|millions|\$\s?000[’'`]?s?)\b", re.I)
NUMBER = re.compile(r"^\(?\$?\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*\)?\s*[*†‡#]*\s*(?:\([a-z0-9]{1,3}\)\s*,?\s*)*[a-z]{0,1}$")


def text(x: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", x)).replace("\xa0", " ").replace("​", " ").replace("‌", "")).strip()


def grid(tr: str) -> list:
    """Cells of a row with their column position, colspans expanded."""
    res, col = [], 0
    for attrs, body in re.findall(r"<t[dh]\b([^>]*)>(.*?)</t[dh]>", tr, re.S | re.I):
        m = re.search(r"colspan\s*=\s*[\"']?(\d+)", attrs, re.I)
        span = int(m[1]) if m else 1
        t = text(body)
        if t:
            res.append((col, col + span - 1, t))
        col += span
    return res


def header_of(rows: list) -> list:
    """The printed header cells of a table, or nothing. A header row names a value or a cost column and holds no large number."""
    for tr in rows[:30]:
        g = grid(tr)
        joined = " ".join(c[2] for c in g)
        if g and len(joined) < 240 and re.search(r"\bvalue\b|\bcost\b", joined, re.I) and not re.search(r"\d,\d{3}", joined):
            return g
    return []


def col_of(label: str):
    for name, rx in COL:
        if re.search(rx, label, re.I):
            return name
    return None


def unit_of(label: str):
    if re.search(r"million|\$\s?m\b", label, re.I):
        return 1e6, label
    if re.search(r"\b000|thousand", label, re.I):
        return 1e3, label
    return None, ""


def page_unit(t: str, pos: int):
    """The nearest unit statement above the table, back to the start of its schedule."""
    z = t[max(0, pos - 60000):pos]
    h = max((m.start() for m in re.finditer(r"schedules? of investments|portfolio of investments|statements? of investments", z, re.I)), default=0)
    sl = text(z[max(0, h - 1500):])
    hits = list(PAGE_UNIT.finditer(sl))
    if not hits:
        return None, ""
    m = hits[-1]
    return (1e6 if "million" in m[1].lower() else 1e3), sl[max(0, m.start() - 20):m.end() + 10]


def as_of(t: str, pos: int) -> str:
    z = text(t[max(0, pos - 90000):pos])
    m = list(re.finditer(MONTH + r" \d{1,2}, \d{4}", z))
    return m[-1][0] if m else ""


def from_ixbrl(t: str, key: str) -> list:
    ctx = {}
    for cid, b in re.findall(r'<xbrli:context id="([^"]+)">(.*?)</xbrli:context>', t, re.S):
        m = re.search(r"InvestmentIdentifierAxis\.domain>(.*?)</", b, re.S)
        if m and re.search(key, m[1], re.I):
            inst = re.search(r"<xbrli:instant>(.*?)<", b)
            if inst:
                ctx[cid] = (html.unescape(m[1]), inst[1])
    rows: dict = {}
    for attrs, val in re.findall(r"<ix:nonFraction([^>]*)>(.*?)</ix:nonFraction>", t, re.S) if ctx else []:
        c = re.search(r'contextRef="([^"]+)"', attrs)
        if not c or c[1] not in ctx:
            continue
        nm = re.search(r'name="([^"]+)"', attrs)[1].split(":")[-1]
        sc = re.search(r'scale="(-?\d+)"', attrs)
        raw = re.sub(r"<[^>]+>", "", val).replace(",", "").strip()
        if re.fullmatch(r"\d*\.?\d+", raw):
            rows.setdefault(c[1], {"name": ctx[c[1]][0], "date": ctx[c[1]][1]})[nm] = float(raw) * 10 ** int(sc[1] if sc else 0)
    out = []
    for cid, r in rows.items():
        q, cs, v = r.get("InvestmentOwnedBalancePrincipalAmount") or r.get("InvestmentOwnedBalanceShares"), r.get("InvestmentOwnedAtCost"), r.get("InvestmentOwnedAtFairValue")
        if q or cs or v:
            out.append(dict(method="iXBRL tag", name=r["name"][:120], qty=q, cost=cs, value=v, date=r["date"], header="iXBRL concepts", unit="scale attribute of the tag", unit_factor=1, raw=json.dumps({k: v_ for k, v_ in r.items() if k not in ("name", "date")})[:300], table=cid, row="", problem=""))
    return out


def from_table(t: str, m, key: str, last: list):
    a = t.rfind("<table", 0, m.start())
    z = t.find("</table>", m.end())
    if a < 0 or z < 0 or t.rfind("</table>", a, m.start()) >= 0 or z - a > 2000000:
        return None
    s = t[a:z]
    trs = re.findall(r"<tr\b[^>]*>(.*?)</tr>", s, re.S | re.I)
    row = next((i for i, tr in enumerate(trs) if key_in(tr, key) and t[a:m.start()].count("<tr") - 1 == i or False), None)
    if row is None:
        row = sum(1 for _ in re.finditer(r"<tr\b", t[a:m.start()], re.I)) - 1
    if row < 0 or row >= len(trs):
        return None
    g = grid(trs[row])
    if not any(re.search(key, c[2], re.I) for c in g):
        return None
    if len(g) == 1 and row + 1 < len(trs):     # the issuer on its own row, its instrument and numbers on the next
        nx = grid(trs[row + 1])
        if any(NUMBER.match(c[2]) for c in nx):
            g = g + nx
    if len(g) == 2 and re.fullmatch(r"\d{1,2}\.\d{1,2}%?", g[1][2]):
        return dict(problem="percent summary, not a position", name=g[0][2][:120], raw=" | ".join(c[2] for c in g)[:300], table=a, row=row, header="", unit="", unit_factor=None, method="table", qty=None, cost=None, value=None, date="")
    h, src = header_of(trs[:row]), "same table"
    if not h and last and last[0]:
        h, src = last[0], "earlier table"
    if h and src == "same table":
        last[:] = [h]
    if not h and not any(NUMBER.match(c[2]) and float(NUMBER.match(c[2])[1].replace(",", "")) >= 100 for c in g):
        return dict(problem="not a position: no amount in the row", name=g[0][2][:120], raw=" | ".join(c[2] for c in g)[:300], table=a, row=row, header="", unit="", unit_factor=None, method="table", qty=None, cost=None, value=None, date="")
    if not h:
        return dict(problem="no column header found", name=g[0][2][:120], raw=" | ".join(c[2] for c in g)[:300], table=a, row=row, header="", unit="", unit_factor=None, method="table", qty=None, cost=None, value=None, date="")
    cols = [(c[0], c[1], col_of(c[2]), unit_of(c[2]), c[2]) for c in h]
    r = dict(method="table", name="", qty=None, cost=None, value=None, commitment=None, date="", table=a, row=row, header=" | ".join(c[2] for c in h)[:200] + f" [{src}]", raw=" | ".join(c[2] for c in g)[:300], problem="")
    r["name"] = next((c[2] for c in g if re.search(key, c[2], re.I)), "")[:140]
    units, pu = {}, page_unit(t, a)
    for c in g:
        if re.search(key, c[2], re.I):
            continue
        v = NUMBER.match(c[2])
        d = re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}(?:\s*-\s*\d{1,2}/\d{1,2}/\d{2,4})?", c[2])
        target = min(cols, key=lambda k: 0 if k[0] <= c[0] <= k[1] or c[0] <= k[1] <= c[1] else min(abs(c[0] - k[0]), abs(c[1] - k[1])))
        name = target[2]
        if d:
            r["date"] = c[2]
        elif v and name in ("qty", "cost", "value", "commitment") and r[name] is None:
            r[name] = float(v[1].replace(",", ""))
            units[name] = target[3]
    cost = re.search(r"cost\s*(?:[:—–-]\s*)?\$\s*([\d,]+(?:\.\d+)?)", r["name"], re.I)
    if cost and r["cost"] is None:
        r["cost"] = float(cost[1].replace(",", ""))
        units["cost"] = units.get("value") or (None, "")
    f = [u[0] for k, u in units.items() if k != "qty" and u and u[0]]
    if f:
        r["unit_factor"], r["unit"] = f[0], next(u[1] for k, u in units.items() if k != "qty" and u and u[0])
    elif pu[0]:
        r["unit_factor"], r["unit"] = pu
    elif "$" in r["header"] and not re.search(r"\b000|thousand|million", r["header"], re.I):
        r["unit_factor"], r["unit"] = 1, "dollars, $ in the column header"
    else:
        r["unit_factor"], r["unit"] = 1, "no unit printed; dollars"
    q = units.get("qty")
    label = next((k[4] for k in cols if k[2] == "qty"), "")
    money_qty = bool(re.search(r"principal|\bpar\b|amount|face", label, re.I)) and not re.search(r"shares", label, re.I)   # par is in the table's money unit
    r["qty_factor"] = q[0] if q and q[0] else (r["unit_factor"] if money_qty else 1)
    if r["qty"] is None and r["cost"] is None and r["value"] is None and r["commitment"] is None:
        r["problem"] = "no number under a known column" if any(NUMBER.match(c[2]) and float(NUMBER.match(c[2])[1].replace(",", "")) >= 100 for c in g) else "not a position: no amount in the row"
    return r


def key_in(tr, key):
    return bool(re.search(key, tr, re.I))


def from_text(t: str, m, key: str):
    """Layouts without table rows: name first, then the footnotes, then shares and value."""
    x = text(t[m.start():m.start() + 1400])
    r = re.match(r"(.{0,170}?(?:\)|\.{2,}|\bDate:[^,]{0,20}, Cost \$[\d,]+[^0-9]{0,4}))\s*(?:\([\da-z]+\)\s*)*[.\s]*(\d[\d,]*)\s+(\d[\d,]*)\b", x)
    if not r or not re.search(r"series|inc|preferred|common", r[1], re.I):
        return None
    z = text(t[max(0, m.start() - 1500):m.start()])
    if not re.search(r"\.{2,}|Cost \$|\(\d\)|\([a-z]\)", r[0]):
        return None
    f, u = page_unit(t, m.start())
    c = re.search(r"cost[:\s]*\$\s*([\d,]+(?:\.\d+)?)", r[0], re.I)
    return dict(method="text layout", name=r[1][:140], qty=float(r[2].replace(",", "")), cost=float(c[1].replace(",", "")) if c else None, value=float(r[3].replace(",", "")), date="", table="text", row=m.start(), header="shares then value, as laid out", unit=u or "no unit printed; dollars", unit_factor=f or 1, raw=x[:300], problem="", qty_factor=1)


def from_pdf(p: Path, key: str) -> list:
    """PDF lines naming the company. Numbers are read right to left against the nearest printed header line above; a line that does not fit goes to review."""
    import pypdfium2
    doc = pypdfium2.PdfDocument(str(p))
    out = []
    labels = [("date", r"date"), ("qty", r"shares|principal|par\b|units"), ("cost", r"cost"), ("value", r"value")]
    for i in range(len(doc)):
        pg = doc[i]
        tp = pg.get_textpage()
        txt = tp.get_text_range()
        tp.close()
        pg.close()
        if not re.search(key, txt, re.I):
            continue
        lines = [re.sub(r"\s+", " ", x).strip() for x in txt.splitlines()]
        u = PAGE_UNIT.search(re.sub(r"\s+", " ", txt))
        unit = (1e6 if "million" in u[1].lower() else 1e3) if u else None
        when = re.findall(MONTH + r" \d{1,2}, \d{4}", txt)
        for j, ln in enumerate(lines):
            if not re.search(key, ln, re.I):
                continue
            nums = [float(x.replace(",", "")) for x in re.findall(r"(?<![\d/.])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\d/])", ln)]
            if not nums:
                continue
            head = next((h for h in reversed(lines[max(0, j - 60):j]) if re.search(r"\b(value|cost)\b", h, re.I) and not re.search(r"\d,\d{3}", h) and len(h) < 200), "")
            order = sorted((re.search(rx, head, re.I).start(), n) for n, rx in labels[1:] if re.search(rx, head, re.I))
            cols = [n for _, n in order]
            r = dict(method="pdf line", name=ln[:140], qty=None, cost=None, value=None, date="", table=f"page {i + 1}", row=j, header=head[:200], unit="" , unit_factor=unit or 1, raw=ln[:300], problem="", qty_factor=1, as_of=when[0] if when else "")
            r["unit"] = u[0][:60] if u else "no unit printed; dollars"
            if cols and len(nums) >= len(cols):
                for n, v in zip(cols, nums[-len(cols):]):
                    r[n] = v
            else:
                r["problem"] = "pdf line does not fit the printed header"
            out.append(r)
    doc.close()
    return out


def scan(job: tuple) -> dict:
    path, key, cache = job
    c = Path(cache) / (hashlib.sha1(path.encode()).hexdigest() + ".json")
    if c.exists():
        return json.loads(c.read_text(encoding="utf-8"))
    res = {"path": path, "rows": [], "other": 0, "error": ""}
    try:
        p = Path(path)
        if p.suffix.lower() == ".pdf":
            res["rows"] += from_pdf(p, key)
        else:
            t = p.read_text(encoding="utf-8", errors="ignore")
            t = re.sub(r"</?(?:table|tr|td|th)\b", lambda x: x[0].lower(), t, flags=re.I)     # older filings use capital tags
            tagged = from_ixbrl(t, key)
            res["rows"] += tagged
            last, seen = [[]], set()
            for m in re.finditer(key, t, re.I):
                r = from_table(t, m, key, last)
                if r is None:
                    r = from_text(t, m, key)
                if r is None:
                    res["other"] += 1
                    continue
                k = (r.get("table"), r.get("row"))
                if k in seen:
                    continue
                seen.add(k)
                r["as_of"] = as_of(t, m.start())
                res["rows"].append(r)
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"[:160]
    c.write_text(json.dumps(res), encoding="utf-8")
    return res


def limit() -> None:
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (1400 * 1024 ** 2, 1400 * 1024 ** 2))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("company")
    ap.add_argument("--folders", nargs="+", required=True)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--dumps", default=str(HERE.parents[2] / "dumps"))
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    cfg = json.loads((HERE / "model_config" / f"{a.company}.json").read_text(encoding="utf-8"))
    key, ev = cfg["company"]["name_regex"], HERE / "out" / a.company / "evidence"
    cache = ev / "table_cache"
    cache.mkdir(parents=True, exist_ok=True)
    files = {}
    for f in a.folders:
        for r in csv.DictReader(open(Path(a.dumps) / f"{f}_manifest.csv", encoding="utf-8", newline="")):
            p = Path(a.dumps) / f / r["file"]
            if p.exists() and p.suffix.lower() in (".htm", ".html", ".pdf") and (not a.only or re.search(a.only, r["form"])):
                files[str(p)] = r
    jobs = sorted(((p, key, str(cache)) for p in files), key=lambda j: Path(j[0]).stat().st_size)
    head = ["accession", "form", "file_date", "filer", "as_of", "method", "name", "qty", "qty_factor", "cost", "value", "commitment", "unit_factor", "date", "header", "unit", "raw", "table", "row", "problem", "url"]
    n = collections.Counter()
    with mp.Pool(a.workers, initializer=limit, maxtasksperchild=25) as pool, open(ev / "parsed_rows.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, head, extrasaction="ignore")
        w.writeheader()
        for i, s in enumerate(pool.imap_unordered(scan, jobs, chunksize=1)):
            m = files[s["path"]]
            n["other_mentions"] += s["other"]
            n["errors"] += bool(s["error"])
            for r in s["rows"]:
                n[r["method"] + (" problem" if r.get("problem") else "")] += 1
                w.writerow({**r, "accession": m["accession"], "form": m["form"], "file_date": m["file_date"], "filer": m["filer"], "url": m["sec_url"]})
            if i % 200 == 0:
                print("scanned", i, "of", len(jobs), flush=True)
    print(json.dumps(dict(n)))


if __name__ == "__main__":
    main()
