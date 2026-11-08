"""Second group of input builders: lender 10-K schedules (iXBRL), S&P tables, H-1B, Form 5500, IRS limits, France. No value is typed here."""
from __future__ import annotations

import csv
import datetime as dt
import html
import json
import re
import urllib.request
from pathlib import Path

from build_inputs import write

UA = "projectx-filings-research testable485@gmail.com"


def _ix(t: str, key: str):
    """(entity name, facts by context) for every context whose member names the company."""
    name = re.search(r'name="dei:EntityRegistrantName"[^>]*>(.*?)</ix:nonNumeric>', t, re.S)
    fund = html.unescape(re.sub(r"<[^>]+>", "", name[1])).strip() if name else ""
    ctx = {}
    for cid, body in re.findall(r'<xbrli:context id="([^"]+)">(.*?)</xbrli:context>', t, re.S):
        m = re.search(r"InvestmentIdentifierAxis\.domain>(.*?)</", body, re.S)
        if m and re.search(key, m[1], re.I):
            inst = re.search(r"<xbrli:instant>(.*?)<", body)
            if inst:
                ctx[cid] = (html.unescape(m[1]), inst[1])
    rows: dict = {}
    if not ctx:
        return fund, rows
    pos: dict = {}
    for mm in re.finditer(r"<ix:nonFraction([^>]*)>(.*?)</ix:nonFraction>", t, re.S):
        attrs, val = mm[1], mm[2]
        c = re.search(r'contextRef="([^"]+)"', attrs)
        if not c or c[1] not in ctx:
            continue
        nm = re.search(r'name="([^"]+)"', attrs)[1].split(":")[-1]
        sc = re.search(r'scale="(-?\d+)"', attrs)
        txt = re.sub(r"<[^>]+>", "", val).replace(",", "").strip()
        if not re.fullmatch(r"\d*\.?\d+", txt):
            continue
        v = float(txt) * 10 ** int(sc[1] if sc else 0)
        rows.setdefault(c[1], {"member": ctx[c[1]][0], "date": ctx[c[1]][1]})[nm] = v
        if nm == "InvestmentOwnedBalancePrincipalAmount":
            pos[c[1]] = mm.start()
        if nm == "InvestmentCompanyFinancialCommitmentToInvesteeFutureAmount":
            rows[c[1]]["commitment"] = v
    for cid, r in rows.items():                # the maturity is printed in the same table row, just before the amounts
        i = pos.get(cid, 0)
        d = re.findall(r"\b(\d{1,2}/\d{1,2}/\d{4}|\d{1,2}/\d{4})\b", re.sub(r"<[^>]+>", " ", t[max(0, i - 4000):i])[-160:])
        if d:
            r["maturity"] = d[-1]
    return fund, rows


def _row_fallback(t: str, key: str):
    """Filings without tagged members: read the table row that names the company."""
    res = []
    unit = 1e3 if re.search(r"in thousands", t, re.I) else 1
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", t, re.S | re.I):
        if not re.search(key, tr, re.I):
            continue
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).replace("\xa0", " ").strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)]
        cells = [c for c in cells if c and c not in ("$", "%", ")")]
        if not re.search(r"term loan", " ".join(cells), re.I) or re.search(r"delayed", " ".join(cells), re.I):
            continue
        nums = [float(c.replace(",", "")) for c in cells if re.fullmatch(r"[\d,]+(\.\d+)?", c) and float(c.replace(",", "")) > 100]
        rate = next((float(c.rstrip("% ")) / 100 for c in cells if re.fullmatch(r"\d\.\d+\s*%?", c)), "")
        mat = next((c for c in cells if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", c)), "")
        if len(nums) >= 3:
            res.append({"member": cells[0], "InvestmentOwnedBalancePrincipalAmount": nums[-3] * unit, "InvestmentOwnedAtCost": nums[-2] * unit, "InvestmentOwnedAtFairValue": nums[-1] * unit, "InvestmentInterestRate": rate, "maturity": mat})
    return res


def _unfunded_rows(t: str, key: str, fund: str) -> list:
    """Filings whose commitments are not tagged: the first short row naming the company, with an amount and no rate. The first one is the latest date."""
    unit = 1e3 if re.search(r"in thousands", t, re.I) else 1
    for m in re.finditer(r"<tr[^>]*>(.*?)</tr>", t, re.S | re.I):
        if not re.search(key, m[1], re.I):
            continue
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).replace("\xa0", " ").strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", m[1], re.S | re.I)]
        cells = [c for c in cells if c and c not in ("$", ")", "%")]
        nums = [float(c.replace(",", "")) for c in cells if re.fullmatch(r"\d[\d,]*", c)]
        if nums and len(cells) <= 8 and "%" not in " ".join(cells) and nums[0] >= 100:
            return [[fund, "table row: " + cells[0][:70], nums[0] * unit]]
    return []


def _r(v):
    return round(v, 6) if isinstance(v, float) else (v if v else "")


def lenders(raw: Path, cfg: dict, out: Path) -> dict:
    key = cfg["company"]["name_regex"]
    cur, prior, unf, seen = [], [], [], {}
    for p in sorted((raw / "sec_10k").glob("*.htm")):
        t = p.read_text(encoding="utf-8", errors="ignore")
        fund, rows = _ix(t, key)
        if not rows and not re.search(r"schedules? of investments", t, re.I):
            continue
        dates = sorted({r["date"] for r in rows.values()})
        if rows:
            last = dates[-1]
        else:
            m = re.search(r'name="dei:DocumentPeriodEndDate"[^>]*>(.*?)<', t)
            last = "2025-12-31"
            fund = fund or p.name
        if seen.get(fund, "") > last:
            continue
        seen[fund] = last
        cur = [x for x in cur if x[0] != fund]
        unf = [x for x in unf if x[0] != fund]
        prior = [x for x in prior if x[0] != fund]
        tagged = [[fund, r["member"][:90], r["commitment"]] for r in rows.values() if r["date"] == last and r.get("commitment")]
        unf += tagged
        src = [r for r in rows.values() if r["date"] == last] or ([dict(x, date=last) for x in _row_fallback(t, key)] if not rows else [])
        for r in src:
            par, fv = r.get("InvestmentOwnedBalancePrincipalAmount"), r.get("InvestmentOwnedAtFairValue")
            if par and (not fv or fv < 0.5 * par or re.search(r"undrawn|delayed|revolv|unfunded", r["member"], re.I)):
                if not tagged:
                    unf.append([fund, r["member"][:90], par])
                continue
            if par and fv:
                cur.append([fund, p.name, r["date"], r["member"][:90], _r(r.get("InvestmentBasisSpreadVariableRate")), _r(r.get("InvestmentInterestRate")), r.get("maturity", ""), par, r.get("InvestmentOwnedAtCost", ""), fv])
        if not any(u[0] == fund for u in unf):
            unf += _unfunded_rows(t, key, fund)
        for r in (x for x in rows.values() if x["date"] < last and x["date"] >= str(int(last[:4]) - 1)):
            par, fv = r.get("InvestmentOwnedBalancePrincipalAmount"), r.get("InvestmentOwnedAtFairValue")
            if par and fv and fv >= 0.5 * par and not re.search(r"undrawn|delayed|revolv|unfunded", r["member"], re.I):
                prior.append([fund, p.name, r["date"], r["member"][:90], _r(r.get("InvestmentBasisSpreadVariableRate")), _r(r.get("InvestmentInterestRate")), r.get("maturity", ""), par, r.get("InvestmentOwnedAtCost", ""), fv])
    head = ["fund", "filing_file", "schedule_date", "instrument", "spread", "interest_rate", "maturity", "par_usd", "cost_usd", "fair_value_usd"]
    return {"credit_bdc_10k_positions.csv": write(out, "credit_bdc_10k_positions.csv", head, cur), "credit_bdc_10k_positions_2024.csv": write(out, "credit_bdc_10k_positions_2024.csv", head, prior),
            "credit_bdc_10k_unfunded.csv": write(out, "credit_bdc_10k_unfunded.csv", ["fund", "commitment", "unfunded_usd"], unf)}


def sp_tables(raw: Path, out: Path) -> dict:
    """The corporate-issuer tables (1, 3 and 10 year). Each number goes to the column whose header is horizontally closest."""
    import pdfplumber
    R = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-", "B+", "B", "B-", "CCC+", "CCC", "CCC-", "CC", "C"]
    a, b = [], []
    with pdfplumber.open(next((raw / "sec_nrsro").glob("*.pdf"))) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            m = re.match(r"Corporate Issuers Ratings (\d+) Year", text)
            if not m:
                continue
            h = int(m[1])
            dates = re.findall(r"\b(\d{1,2}/\d{1,2}/\d{4})\b", text)
            cohort = re.findall(r"(\d{4})", " ".join(dates))[:2]
            lines: dict = {}
            for w in page.extract_words():
                lines.setdefault(round(w["top"]), []).append(w)
            head = next(sorted(v, key=lambda w: w["x0"]) for k, v in sorted(lines.items()) if {"Default", "AAA", "BBB"} <= {w["text"] for w in v})
            cols = [((w["x0"] + w["x1"]) / 2, w["text"]) for w in head if w["text"] in R or w["text"] in ("Default", "Paid")]
            names = [c[1] for c in cols]
            for k, v in sorted(lines.items()):
                v = sorted(v, key=lambda w: w["x0"])
                if k <= head[0]["top"] + 5 or v[0]["text"] not in R or len(v) < 2 or not v[1]["text"].replace(",", "").isdigit():
                    continue
                cells = dict.fromkeys(names, 0.0)
                for w in v[2:]:
                    if w["text"].endswith("%"):
                        x = (w["x0"] + w["x1"]) / 2
                        cells[min(cols, key=lambda c: abs(c[0] - x))[1]] = float(w["text"].rstrip("%")) / 100
                n, rt = int(v[1]["text"].replace(",", "")), v[0]["text"]
                a.append([h, rt, R.index(rt) + 1, n, cells.get("Default", 0.0), round(cells.get("Default", 0.0) * n), cells.get("Paid", 0.0), cells.get(rt, 0.0)])
                b.append([h, rt, n] + [cells.get(x, 0.0) for x in R] + [cells.get("Default", 0.0), cells.get("Paid", 0.0)])
    return {"sp_nrsro_corporate_default_rates.csv": write(out, "sp_nrsro_corporate_default_rates.csv", ["horizon_years", "rating", "notch", "n_outstanding", "default_rate", "defaults_implied", "withdrawn_paid_off_rate", "same_rating_rate"], a),
            "sp_nrsro_transition_matrices.csv": write(out, "sp_nrsro_transition_matrices.csv", ["horizon_years", "from_rating", "n_outstanding"] + R + ["Default", "Withdrawn/Paid Off"], b)}


def h1b(cache: Path, manifest: Path, cfg: dict, out: Path) -> dict:
    cache.mkdir(parents=True, exist_ok=True)
    urls = re.findall(r"https://[^,\"]*h1b_datahubexport[^,\"]*\.csv", manifest.read_text(encoding="utf-8")) if manifest.exists() else []
    body = []
    for u in sorted(set(urls)):
        f = cache / u.rsplit("/", 1)[-1]
        if not f.exists():
            req = urllib.request.Request(u, headers={"User-Agent": UA})
            f.write_bytes(urllib.request.urlopen(req, timeout=120).read())
    for f in sorted(cache.glob("*.csv")):
        with open(f, encoding="latin-1", newline="") as fh:
            for r in csv.DictReader(fh):
                r = {re.sub(r"\W+", " ", k).strip().lower(): (v or "").strip() for k, v in r.items() if k}
                who = r.get("employer") or r.get("employer petitioner name") or ""
                if re.search(cfg["company"]["name_regex"], who, re.I):
                    g = lambda k: int(r.get(k, "0").replace(",", "") or 0)                 # noqa: E731
                    body.append([r.get("fiscal year") or r.get("fiscal year "), who, g("initial approval") or g("initial approvals"), g("initial denial") or g("initial denials"), g("continuing approval") or g("continuing approvals"), g("continuing denial") or g("continuing denials")])
    return {"uscis_h1b.csv": write(out, "uscis_h1b.csv", ["fiscal_year", "employer", "initial_approval", "initial_denial", "continuing_approval", "continuing_denial"], body)}


def form5500(emp: Path, cfg: dict, out: Path) -> dict:
    co = cfg["company"]["employee_company"]
    rd = lambda n: [r for r in csv.DictReader(open(emp / n, encoding="utf-8", newline="")) if r.get("company") == co]      # noqa: E731
    f = rd("form5500_rows.csv")
    latest: dict = {}
    for r in f:
        k = (r["FORM_PLAN_YEAR_BEGIN_DATE"][:4], r["SPONS_DFE_PN"])
        latest[k] = r if k not in latest or r["ACK_ID"] > latest[k]["ACK_ID"] else latest[k]
    n1 = write(out, "dol_form5500.csv", ["FORM_PLAN_YEAR_BEGIN_DATE", "SPONS_DFE_PN", "TOT_ACT_PARTCP_BOY_CNT", "TOT_ACTIVE_PARTCP_CNT"],
               [[r["FORM_PLAN_YEAR_BEGIN_DATE"], r["SPONS_DFE_PN"], r["TOT_ACT_PARTCP_BOY_CNT"], r["TOT_ACTIVE_PARTCP_CNT"]] for k, r in sorted(latest.items())])
    pp = {}
    for k, r in latest.items():
        if r["SPONS_DFE_PN"] == cfg["k401_plan"]:
            pp[int(k[0])] = [k[0], cfg["k401_plan"], r["TOT_ACT_PARTCP_BOY_CNT"], r["TOT_ACTIVE_PARTCP_CNT"]]
    for r in rd("form5500sf_rows.csv"):
        y = int(r["SF_PLAN_YEAR_BEGIN_DATE"][:4]) if "SF_PLAN_YEAR_BEGIN_DATE" in r else None
        if y and y not in pp and r.get("SF_PLAN_NUM", r.get("SF_PN")) == cfg["k401_plan"]:
            pp[y] = [str(y), cfg["k401_plan"], r.get("SF_TOT_ACT_PARTCP_BOY_CNT", ""), r.get("SF_TOT_ACT_PARTCP_EOY_CNT", "")]
    n2 = write(out, "plan_participants.csv", ["plan_year", "plan_number", "active_boy", "active_eoy"], [pp[y] for y in sorted(pp)])
    w_ = [r for r in csv.DictReader(open(emp.parent / "data" / "welfare_plans_by_year.csv", encoding="utf-8", newline="")) if r["company"] == co]
    write(out, "welfare_plans.csv", ["plan_year", "plan_number", "plan_kind", "active_participants_eoy"], [[r["plan_year"], r["plan_number"], r["plan_kind"], r["active_participants_eoy"]] for r in w_])
    h = [r for r in rd("schedule_h_rows.csv") if r.get("SCH_H_PN") == cfg["k401_plan"]]
    last: dict = {}
    for r in h:
        y = r["SCH_H_PLAN_YEAR_BEGIN_DATE"][:4]
        last[y] = r if y not in last or r["ACK_ID"] > last[y]["ACK_ID"] else last[y]
    n3 = write(out, "dol_5500_sch_h.csv", ["SCH_H_PLAN_YEAR_BEGIN_DATE", "PARTICIPANT_CONTRIB_AMT", "EMPLR_CONTRIB_INCOME_AMT", "NET_ASSETS_EOY_AMT"],
               [[r["SCH_H_PLAN_YEAR_BEGIN_DATE"], r["PARTICIPANT_CONTRIB_AMT"], r["EMPLR_CONTRIB_INCOME_AMT"], r["NET_ASSETS_EOY_AMT"]] for y, r in sorted(last.items())])
    return {"dol_form5500.csv": n1, "plan_participants.csv": n2, "dol_5500_sch_h.csv": n3}


def france(raw: Path, out: Path) -> dict:
    d = json.load(open(next((raw / "france").glob("*.json")), encoding="utf-8"))
    r = d["results"][0] if "results" in d else d
    fin = r.get("finances") or {}
    y = max(fin) if fin else ""
    return {"fr_subsidiary.csv": write(out, "fr_subsidiary.csv", ["entity", "siren", "api_year_label", "turnover_eur", "net_result_eur"], [[r.get("nom_raison_sociale") or r.get("nom_complet"), r.get("siren"), y, fin.get(y, {}).get("ca"), fin.get(y, {}).get("resultat_net")]])}


def ocr_pdfs(raw: Path, cache: Path) -> None:
    """Scanned accounts to text, one file per PDF, so a rerun reuses the result."""
    import subprocess
    import pypdfium2
    cache.mkdir(parents=True, exist_ok=True)
    for pdf in sorted((raw / "companies_house").glob("*.pdf")):
        txt = cache / (pdf.stem + ".txt")
        if txt.exists():
            continue
        doc, parts = pypdfium2.PdfDocument(str(pdf)), []
        for i in range(len(doc)):
            png = cache / f"{pdf.stem}_{i}.png"
            doc[i].render(scale=2.8).to_pil().save(png)
            parts.append(subprocess.run(["tesseract", str(png), "stdout", "--psm", "6"], capture_output=True, text=True).stdout)
            png.unlink()
        txt.write_text("\n".join(parts), encoding="utf-8")


def uk_accounts(cache: Path, out: Path) -> dict:
    """Each period from the latest accounts that print it: the first column of its own accounts, the second column of the next ones."""
    tok = re.compile(r"\(?\d{1,3}(?:,\d{3})+\)?|(?<=\s)[-\u2014\u2013](?=\s|$)")
    val = lambda t: 0.0 if t in "-\u2014\u2013" else (-1 if t[0] == "(" else 1) * float(re.sub(r"[(),]", "", t))     # noqa: E731
    pats = {"turnover_gbp": r"Turnover\b", "operating_profit_gbp": r"Operating profit\b", "wages_salaries_gbp": r"Wages and salaries\b", "social_security_gbp": r"Social security costs\b",
            "pension_gbp": r"(?:Other )?[Pp]ension costs\b", "share_based_payment_gbp": r"Share.based payments?\b"}
    files = sorted(cache.glob("*.txt"), key=lambda f: re.search(r"to_(\d{4}-\d{2}-\d{2})", f.stem)[1])
    ends = [dt.date.fromisoformat(re.search(r"to_(\d{4}-\d{2}-\d{2})", f.stem)[1]) for f in files]
    rows: dict = {}
    for i, f in enumerate(files):
        t = f.read_text(encoding="utf-8")
        got = {}
        for k, p in pats.items():
            for line in t.splitlines():
                m = re.match(r"\W*" + p, line.strip())
                if m:
                    rest = " " + line.strip()[m.end():] + " "
                    if re.search(r"[A-Za-z]{3,}", rest):
                        continue                               # a sentence, not a table line
                    n = tok.findall(rest)
                    if len(n) > 2 and not re.search(r"\d", n[0]):       # a stray dash before the first figure
                        n = n[1:]
                    if len(n) >= 2 and re.search(r"\d", "".join(n)):
                        got[k] = [val(x) for x in n[:2]]
                        break
        if "operating_profit_gbp" not in got and "turnover_gbp" in got:     # the line was not read: turnover less administrative expenses
            for line in t.splitlines():
                n = tok.findall(" " + line + " ")
                if re.match(r"\W*Administrative expenses", line.strip()) and len(n) >= 2:
                    got["operating_profit_gbp"] = [got["turnover_gbp"][i] + val(n[i]) for i in (0, 1)]
                    break
        head = re.search(r"Headcount[^\n]*", t)
        staff = re.findall(r"(\d+) Average", head[0]) if head else []
        for col, j in ((0, i), (1, i - 1)):
            if j < 0 or not got:
                continue
            r = rows.setdefault(ends[j], {"period_end": ends[j].isoformat()})
            months = round((ends[j] - ends[j - 1]).days / 30.4) if j else 12
            r["months"] = months
            for k, v in got.items():
                r[k] = v[col]
            if len(staff) > col:
                r["avg_employees"] = staff[col]
            r["source_key"] = f.stem
    head = ["period_end", "months", "turnover_gbp", "operating_profit_gbp", "avg_employees", "wages_salaries_gbp", "share_based_payment_gbp", "social_security_gbp", "pension_gbp", "source_key"]
    keep = [e for e in sorted(rows) if rows[e].get("turnover_gbp")]
    return {"uk_companies_house_accounts.csv": write(out, "uk_companies_house_accounts.csv", head, [[rows[e].get(k, "") for k in head] for e in keep])}


def irs_limits(raw: Path, out: Path) -> dict:
    """Elective deferral limit by year: the current table and the prior-years PDF."""
    import pdfplumber
    t = html.unescape(re.sub(r"<[^>]+>", " ", (raw / "irs" / "cola_increases_dollar_limitations.html").read_text(encoding="utf-8", errors="ignore")))
    t = re.sub(r"\s+", " ", t)
    res = {}
    years = re.search(r"Limit\s+((?:20\d\d\s+){3,})", t)
    vals = re.search(r"Elective deferrals\s+((?:[\d,]+\s+){3,})", t)
    if years and vals:
        for y, v in zip(years[1].split(), vals[1].split()):
            res[int(y)] = int(v.replace(",", ""))
    with pdfplumber.open(next((raw / "irs").glob("*.pdf"))) as pdf:
        for p in pdf.pages:
            lines = (p.extract_text() or "").splitlines()
            ys = next((re.findall(r"20\d\d", ln) for ln in lines if len(re.findall(r"\b20\d\d\b", ln)) >= 3), None)
            v = next((re.findall(r"\d{2},\d{3}", ln) for ln in lines if re.search(r"402\(g\)|[Ee]lective", ln) and re.search(r"\d{2},\d{3}", ln)), None)
            if ys and v:
                for y, x in zip(ys, v):
                    res.setdefault(int(y), int(x.replace(",", "")))
    return {"irs_402g_limits.csv": write(out, "irs_402g_limits.csv", ["year", "elective_deferral_limit_402g"], sorted(res.items()))}
