"""Build a Databricks-style "financials year by year" report for one private company, from the Form D database and the
filings that mention it. Anything the sources cannot give is shown as a placeholder (R1, R2 ...) and listed at the end
with the form or record needed to fill it.

Steps (one command):
  1. Form D          SQL on formd.duckdb: offerings (date of first sale, offered, sold, investors, equity/debt), people,
                     incorporation, SPVs set up to buy the company's shares.
  2. Filings         the company's prepared filings (coref_verifier/data: filing + kept mentions + .meta.json from
                     prepare_targeted.py; the existing pipeline's output). Prepared now if missing.
  3. Relationships   steps/relationships.py on every filing (with the direction check). Cached per filing.
  4. Facts           NEW: GPT-4.1 reads every paragraph that names the company, tables included, and returns
                     (a) fund holdings: holder, vehicle, shares, cost, fair value, as-of date;
                     (b) dated facts: trading volume, revenue, valuation, legal events, regulatory status ...
                     Every row keeps a verbatim quote; quotes and numbers are checked against the text. Cached per filing.
  5. Merge           de-duplicate amendments and repeated rows; price per share = fair value / shares.
  6. Report          HTML + PDF (Chrome headless) and CSVs of every table, in out/<company>/.

Usage:
  company-finder/.venv/bin/python coref_experiment/company_report/build_report.py kalshi --cik 0001806928
        [--name "Kalshi Inc."] [--formd-db PATH] [--model openai/gpt-4.1] [--refresh-facts] [--rerun-relationships]
        [--no-llm]   (Form D + cached outputs only)
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import html
import json
import re
import shutil
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXP = HERE.parent
ROOT = EXP.parent
APP = EXP / "coref_verifier"
sys.path.insert(0, str(EXP / "steps"))
import shared as s  # noqa: E402
import model_credit  # noqa: E402
import model_financials  # noqa: E402
import model_report  # noqa: E402
import relationships as rel  # noqa: E402  (quote_in_text)

c, d = s.c, s.d
DEFAULT_DB = ROOT / "Project X Resources" / "formd_db_2026-10-20" / "formd.duckdb"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
if not Path(CHROME).exists():
    CHROME = shutil.which("brave") or shutil.which("google-chrome") or shutil.which("chromium") or CHROME
FORECAST_YEARS = 5

# ---------------------------------------------------------------------------------------------- placeholders
# id -> (what is missing, form / record that gives it, where to get it, what it unlocks)
REQUIREMENTS = {
    "R1": ("Revenue, revenue run-rate, growth", "Company press releases and CEO interviews (run-rate / ARR statements); "
           "Form D revenue range",
           "Company newsroom, news archives", "Revenue table, growth, run-rate curve, every revenue-based ratio"),
    "R2": ("Revenue uncertainty range", "Two or more revenue disclosures per year (same sources as R1)", "As R1",
           "The +/- range and the evidence grade A-C"),
    "R3": ("Employees (average per year)", "DOL Form 5500 / 5500-SF (401(k) and health plans, by EIN); USCIS H-1B Employer Data Hub; "
           "DOL LCA disclosures; company headcount statements", "askebsa.dol.gov (EFAST2), uscis.gov, dol.gov/agencies/eta/foreign-labor",
           "Headcount section, cost and EBITDA proxies, revenue per employee"),
    "R4": ("Company valuation per round (post-money) and its revenue multiple",
           "Company press releases on each round; total share count (Delaware certificate of incorporation, S-1 if it lists); "
           "N-PORT / N-CSR fund marks", "Newsroom, Delaware Division of Corporations, SEC EDGAR",
           "Valuation column, multiples, turns the per-share marks into a company value"),
    "R5": ("Gross margin and cost per employee", "Foreign subsidiary accounts (e.g. UK Companies House); 10-K cost structure of "
           "comparable public companies", "find-and-update.company-information.service.gov.uk, SEC EDGAR",
           "Calibration of the cost model"),
    "R6": ("EBITDA proxy, FCF proxy, cash", "Needs R1 + R3 + R5 (model output); interest needs R7",
           "-", "Costs, EBITDA and cash section; runway at each round"),
    "R7": ("Debt raised and interest rate", "UCC financing statements; BDC 10-K / 10-Q schedules of investments; N-PORT loan rows; "
           "press releases", "State Secretary of State (DE, NY) UCC search, SEC EDGAR",
           "Debt column, interest in the FCF proxy"),
    "R8": ("Customers / users", "Company press releases; court declarations", "Newsroom, PACER / CourtListener",
           "Customer table, revenue per customer"),
    "R9": ("Usage or volume history", "Company statements; public usage data; regulator filings", "Company site, regulator site",
           "Revenue model when no revenue statement exists"),
    "R10": ("Forecast (5 years)", "R1 history + SEC XBRL frames API for a reference class of public comparables", "data.sec.gov/api/xbrl/frames",
            "Forecast section and forecast rows of the yearly table"),
    "R11": ("Funding in years with no Form D", "Form D / D/A on EDGAR not yet in the database; press releases on rounds",
            "SEC EDGAR full-text search, newsroom", "Complete funding table"),
    "R12": ("Quarterly fair-value marks", "N-PORT (monthly holdings, quarterly public) and N-CSR / N-CSRS of every fund that holds the "
            "company", "SEC N-PORT data sets, EDGAR", "A full valuation curve instead of a few points"),
    "R13": ("Fiscal year end", "Audited financial statements or a company statement", "-",
            "Fiscal-year columns; this report uses calendar years"),
}


class Placeholders:
    def __init__(self) -> None:
        self.used: list[str] = []

    def __call__(self, rid: str, label: str = "") -> str:
        if rid not in self.used:
            self.used.append(rid)
        return f'<span class="ph" title="{html.escape(REQUIREMENTS[rid][0])}">{html.escape(label) + " " if label else ""}{rid}</span>'


# ---------------------------------------------------------------------------------------------- small helpers
def esc(x) -> str:
    return html.escape("" if x is None else str(x))


def money(v, dash: str = "—") -> str:
    if v is None or v == "":
        return dash
    v = float(v)
    a = abs(v)
    sign = "−" if v < 0 else ""
    if a >= 1e9:
        return f"{sign}${a / 1e9:.2f}B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:.1f}M"
    if a >= 1e3:
        return f"{sign}${a / 1e3:.0f}K"
    return f"{sign}${a:,.0f}"


def num(v) -> str:
    return "—" if v in (None, "") else f"{float(v):,.0f}"


def as_date(x) -> dt.date | None:
    if x is None or x == "":
        return None
    if isinstance(x, dt.datetime):
        return x.date()
    if isinstance(x, dt.date):
        return x
    m = re.match(r"(\d{4})-(\d{2})(?:-(\d{2}))?", str(x))
    if not m:
        return None
    try:
        return dt.date(int(m[1]), int(m[2]), int(m[3] or 1))
    except ValueError:      # model returned an impossible date: treat as undated
        return None


def repeat(row: dict) -> str:
    n = row.get("sources", 1)
    return f" (+{n - 1} repeat)" if n > 1 else ""


def fmt_date(x) -> str:
    v = as_date(x)
    return v.strftime("%d %b %Y") if v else "date not stated"


LEGAL = re.compile(r"[,\s]+(?:inc|incorporated|corp|corporation|co|llc|l\.l\.c|ltd|limited|l\.?p|plc|pbc|s\.?a|ag|gmbh|n\.?v|b\.?v)\.?$", re.I)


def short_name(name: str, cfg: dict) -> str:
    """The name used in the text: model_config "name", else the legal name without its corporate suffix ("Acme Labs, Inc." -> "Acme Labs")."""
    s = str(cfg.get("name") or "").strip()
    if s:
        return s
    s = name.strip()
    while LEGAL.search(s):
        s = LEGAL.sub("", s).strip()
    return s or name


def norm_name(x: str) -> str:
    x = re.sub(r"[^a-z0-9 ]", " ", (x or "").lower())
    x = re.sub(r"\b(inc|llc|lp|l p|corp|corporation|co|ltd|the|fund)\b", " ", x)
    return re.sub(r"\s+", " ", x).strip()


def digits_in(n, text: str) -> bool:
    """Is the number n written in the text (with or without thousands separators)?"""
    if n in (None, ""):
        return True
    t = re.sub(r"[,\s$]", "", text)
    v = abs(float(n))                  # a loss prints as (153)
    cands = {f"{v:.0f}", f"{v:.1f}", f"{v:.2f}", f"{v / 1000:.0f}", f"{v / 1e6:.0f}", f"{v / 1e6:.1f}", f"{v / 1e9:.1f}"}
    return any(x in t for x in cands if x not in ("0", "0.0"))


# ---------------------------------------------------------------------------------------------- 1. Form D
def load_formd(db_path: Path, cik: str, spv_key: str) -> dict:
    import duckdb

    db = duckdb.connect(str(db_path), read_only=True)

    def rows(sql: str, params=()) -> list[dict]:
        cur = db.execute(sql, list(params))
        cols = [x[0] for x in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    comp = rows("SELECT * FROM main.companies WHERE cik = ?", [cik])
    if not comp:
        raise SystemExit(f"CIK {cik} is not in {db_path}")
    filings = rows("""SELECT accession_number, filing_date, submission_type, sale_date, file_num, is_amendment,
        previous_accession_number, is_equity, is_debt, is_option_to_acquire, is_business_combination, other_type_desc,
        revenue_range, total_offering_amount, total_amount_sold, total_remaining, total_investors, minimum_investment,
        sales_amt_clarification, gross_proceeds_clarification, year_of_inc, issuer_name
        FROM main.filings WHERE cik = ? ORDER BY filing_date""", [cik])
    # an amendment restates the whole offering: keep the latest filing of each offering (file number, else accession)
    offerings: dict = {}
    for f in filings:
        key = f["file_num"] or f["previous_accession_number"] or f["accession_number"]
        offerings[key] = f
    offs = sorted(offerings.values(), key=lambda f: (as_date(f["sale_date"]) or as_date(f["filing_date"])))
    people_rows = rows("""SELECT p.first_name, p.last_name, p.relationship_1, p.relationship_2, p.relationship_3,
        f.filing_date FROM main.related_persons p JOIN main.filings f USING (accession_number) WHERE f.cik = ?""", [cik])
    people: dict = {}
    for p in people_rows:
        name = re.sub(r"\s+", " ", f"{p['first_name'] or ''} {p['last_name'] or ''}").strip().title()
        key = " ".join(sorted(norm_name(name).split()))      # 'Antani Snehal' = 'Snehal Antani'
        e = people.setdefault(key, {"name": name, "roles": set(), "first": p["filing_date"], "last": p["filing_date"], "n": 0})
        e["roles"].update(r for r in (p["relationship_1"], p["relationship_2"], p["relationship_3"]) if r)
        e["first"], e["last"] = min(e["first"], p["filing_date"]), max(e["last"], p["filing_date"])
        e["n"] += 1
    spvs = [r for r in rows("""SELECT cik, company_name, total_raised, total_investors, first_filing, last_filing
        FROM main.companies WHERE category = 'fund' AND company_name ILIKE ? ORDER BY first_filing""", [f"%{spv_key}%"])
            if re.search(rf"\b{re.escape(spv_key)}\b", r["company_name"], re.I)]
    db_until = rows("SELECT max(filing_date) AS m FROM main.filings")[0]["m"]
    years_inc = [f["year_of_inc"] for f in filings if f["year_of_inc"] and str(f["year_of_inc"]).isdigit()]
    return {"company": comp[0], "filings": filings, "offerings": offs, "people": sorted(people.values(), key=lambda p: p["first"]),
            "spvs": spvs, "db_until": db_until, "year_of_inc": int(years_inc[0]) if years_inc else None}


# ---------------------------------------------------------------------------------------------- 2. filings
def prepared_filings(company: str, prepare: bool, extra: list[str] | None = None, only: set | None = None) -> list[dict]:
    """Filings of one dump folder plus any extra folders (a form set downloaded separately, e.g. databricks_ncsr). Results
    are cached per filing, so adding a folder only costs the new filings."""
    metas = []
    for folder in [company] + (extra or []):
        found = sorted((APP / "data" / "companies").glob(f"{folder}__*_mentions.meta.json"))
        if not found and prepare:
            print(f"no prepared filings for {folder}: running prepare_targeted.py")
            cmd = [sys.executable, str(APP / "tools" / "prepare_targeted.py"), folder] + (["--link"] if folder != company else [])
            subprocess.run(cmd, check=True)
            found = sorted((APP / "data" / "companies").glob(f"{folder}__*_mentions.meta.json"))
        metas += found
    out, seen = [], set()
    for mf in metas:
        m = json.loads(mf.read_text(encoding="utf-8"))
        m["mentions_csv"] = str(mf.with_name(mf.name.replace(".meta.json", ".csv")))
        m["pdf"] = str(APP / "data" / "pdfs" / m["document"])
        m["stem"] = Path(m["document"]).stem
        if m["stem"] in seen or (only is not None and m["stem"] not in only):
            continue
        seen.add(m["stem"])
        out.append(m)
    return sorted(out, key=lambda m: (m["file_date"], m["form"]))


# ---------------------------------------------------------------------------------------------- 3. relationships
def run_relationships(filings: list[dict], out: Path, model: str, rerun: bool, no_llm: bool) -> list[dict]:
    rdir = out / "relationships"
    rdir.mkdir(parents=True, exist_ok=True)

    def one(m: dict) -> str:
        csv_path = rdir / f"{m['stem']}_relationships.csv"
        run_json = rdir / f"{m['stem']}_relationships_run.json"
        finished = not run_json.exists() or json.loads(run_json.read_text(encoding="utf-8")).get("meta", {}).get("output_complete", True)
        if csv_path.exists() and finished and not rerun:   # a run with failed chunks is done again, not remembered as complete
            return "cached"
        if no_llm:
            return "skipped (--no-llm)"
        cmd = [sys.executable, str(EXP / "steps" / "relationships.py"), m["pdf"], "--mentions", m["mentions_csv"],
               "--filer", m["filer"], "--form", m["form"], "--filing-date", m["file_date"], "--model", model,
               "--out-dir", str(rdir)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        return "ok" if r.returncode == 0 else "FAILED: " + (r.stderr or r.stdout)[-300:]

    with ThreadPoolExecutor(max_workers=3) as pool:
        for m, st in zip(filings, pool.map(one, filings)):
            print(f"  relationships {m['form']:7} {m['file_date']} {m['filer'][:34]:34} {st}", flush=True)
    rows = []
    for m in filings:
        p = rdir / f"{m['stem']}_relationships.csv"
        if p.exists():
            for r in csv.DictReader(open(p, encoding="utf-8")):
                rows.append({**r, "form": m["form"], "filing_date": m["file_date"], "filer": m["filer"], "sec_url": m["sec_url"]})
    return rows


# ---------------------------------------------------------------------------------------------- 4. facts
METRICS = ["trading_volume", "revenue", "revenue_run_rate", "valuation", "funding_round", "fund_transaction", "employees",
           "customers_users", "debt", "legal_event", "regulatory_status", "product_business", "person_role", "investor_link", "other"]
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE_HEAD = re.compile(rf"(?:as of|as at|at|through|ended|ending|period ended)\s+(?:{MONTHS})\s+\d{{1,2}},\s+\d{{4}}", re.I)
TABLE_HEAD = re.compile(r"schedule of investments|restricted securit|portfolio of investments|statement of assets|"
                        r"fair value at|investments in|holdings", re.I)
UNITS = ["USD", "USD_per_week", "USD_per_year", "shares", "count", "percent", "none"]

FACTS_SYSTEM = """You extract dated FACTS about one private company, TARGET = {target}, from paragraphs of an SEC filing.
{filing}
Each paragraph is shown as [P<n>|p<page>] text. Lines starting with [ctx] are neighbouring paragraphs (table headings,
titles, previous rows) given only to read dates, units and column names. Table rows are cells separated by " | ".

Return two lists.

holdings: one row for every position in TARGET that a fund or investor reports (schedule of investments, list of
restricted securities, holdings list, transactions with portfolio companies). Fields:
  para_no; holder (the fund/investor that owns the position: the filer when it is the filer's own table; when one filing
  covers several funds or series, the fund or series whose schedule this row sits in: a [schedule] line gives the nearest
  schedule heading above the row, take the name from it, never the first fund named in the filing);
  vehicle ("direct" or the name of the SPV / feeder through which it is held, e.g. "ABC Capital I, LLC - Class A");
  security (classes held, short, e.g. "23,382 Common + 38,969 Series E Preferred");
  shares (TOTAL number of TARGET shares in the row, all classes added; null if no share count is given, e.g. an SPV
  interest, and null for a loan: a loan's par or principal is never shares; units or capital commitments of a fund or SPV
  are not shares of TARGET either); cost_usd; fair_value_usd; commitment_usd (the undrawn amount of an unfunded loan
  commitment; put it here and leave cost_usd and fair_value_usd null unless the row states them separately);
  position_type (equity = shares, preferred, warrants or other stock of TARGET held directly; funded_loan = a drawn term
  loan, revolver or note; unfunded_commitment = the undrawn part of a loan or delayed draw commitment;
  spv_interest = an interest in an SPV, partnership or feeder that holds TARGET; other);
  shared_with (other companies the same position also gives exposure to, e.g. "SpaceX" for an SPV described as exposure
  to TARGET and SpaceX; "" when TARGET is the only one);
  value_scale and shares_scale (1, 1000 or 1000000: what the printed money figures and the printed share count must be
  multiplied by; read it from the row's table heading or column names, e.g. "(in thousands)", "Value (000)", "$000s",
  "Shares (000)"; a [units] line lists such headings found further up the document: use one only if it clearly belongs
  to this table; a percentage or per-share price is never scaled);
  pct_net_assets (number, e.g. 6.26 for 6.26%); acquisition_date;
  as_of_date (the date the table values are as of, read from its heading or column, e.g. "as of March 31, 2026";
  a [dates] line lists date headings found further up the document: use one only if it is clearly the heading of
  this table (a schedule of investments / holdings heading); "" when unsure - NEVER use the filing date as a guess);
  evidence; confidence.
  Copy shares, cost_usd, fair_value_usd and commitment_usd exactly as printed in the row (do NOT multiply: the code
  applies value_scale and shares_scale). null when a value is not given.
  Which money column is which: three money columns are par or principal, cost, fair value (par is not stored). Two money
  columns before the % of net assets are cost, then fair value. A figure written "$112.4 million" is 112.4 with
  value_scale 1000000. One money column
  alone is fair_value_usd (a benefit plan's "current value", a "value" or "market value" column) unless its heading says
  cost. In a filing's list of unfunded commitments, a row with only a borrower, perhaps a loan type or expiry date, and one
  amount or the same amount repeated, with no interest rate, is unfunded_commitment: put the amount in commitment_usd once.
  shares only when the text gives a number of shares of TARGET. Lists of brokers, counterparties or principal
  transactions (Form N-CEN) give a dollar amount bought or sold, not a holding: report that as a fact with metric
  fund_transaction, never as a holding.

facts: every other fact about TARGET (or its subsidiaries). One row per fact:
  para_no; metric (one of: {metrics}); value_number (number or null); unit (one of: {units});
  as_of_date (the date the fact refers to: the event or ruling date for a legal_event, the period end for a volume or
  revenue figure; YYYY-MM-DD, or YYYY-MM / YYYY when only that is given; "" if no date is stated);
  value_text (one plain-English sentence, max 30 words, stating the fact with its date);
  evidence; confidence.
  legal_event = each lawsuit, ruling, injunction, appeal or regulator action involving TARGET, as a separate row.
  person_role = a person's role at TARGET (director, adviser, founder). investor_link = an investor or fund manager
  named as backing or managing an investment in TARGET without amounts. valuation = a value of the WHOLE company
  (post-money, price per share of a round); a fund's position value belongs in holdings only.

Rules:
- Only facts about TARGET. Skip facts only about the filer or other companies.
- Copy numbers exactly; do not compute, convert currencies or round.
- evidence: a short VERBATIM quote (max 30 words) from the [P<n>] paragraph (or its [ctx] lines for a date or
  heading) that contains the number or the event.
- confidence 0-100.
Return {{"holdings": [...], "facts": [...]}}; empty lists when there is nothing."""

_NUM = {"type": ["number", "null"]}
CARD_LINES = 12   # lines per year card; the full list is the holdings table
ETF_BOILERPLATE = ("other", "legal_event", "regulatory_status", "product_business")
POSITION_TYPES = ["equity", "funded_loan", "unfunded_commitment", "spv_interest", "other"]
SCALES = [1, 1000, 1000000]
FACTS_SCHEMA = {
    "name": "facts", "strict": True,
    "schema": {
        "type": "object", "additionalProperties": False, "required": ["holdings", "facts"],
        "properties": {
            "holdings": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["para_no", "holder", "vehicle", "security", "shares", "cost_usd", "fair_value_usd", "commitment_usd",
                             "position_type", "shared_with", "value_scale", "shares_scale",
                             "pct_net_assets", "acquisition_date", "as_of_date", "evidence", "confidence"],
                "properties": {"para_no": {"type": "integer"}, "holder": {"type": "string"}, "vehicle": {"type": "string"},
                               "security": {"type": "string"}, "shares": _NUM, "cost_usd": _NUM, "fair_value_usd": _NUM,
                               "commitment_usd": _NUM, "shared_with": {"type": "string"},
                               "position_type": {"type": "string", "enum": POSITION_TYPES},
                               "value_scale": {"type": "integer", "enum": SCALES}, "shares_scale": {"type": "integer", "enum": SCALES},
                               "pct_net_assets": _NUM, "acquisition_date": {"type": "string"},
                               "as_of_date": {"type": "string"}, "evidence": {"type": "string"},
                               "confidence": {"type": "integer"}}}},
            "facts": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["para_no", "metric", "value_number", "unit", "as_of_date", "value_text", "evidence", "confidence"],
                "properties": {"para_no": {"type": "integer"}, "metric": {"type": "string", "enum": METRICS},
                               "value_number": _NUM, "unit": {"type": "string", "enum": UNITS},
                               "as_of_date": {"type": "string"}, "value_text": {"type": "string"},
                               "evidence": {"type": "string"}, "confidence": {"type": "integer"}}}},
        }}}


def parse_facts(content: str) -> tuple[dict, bool]:
    try:
        o = json.loads(content)
        return {"holdings": o.get("holdings", []), "facts": o.get("facts", [])}, True
    except (ValueError, AttributeError):
        return {"holdings": [], "facts": []}, False


def target_id(scope, target: str) -> str | None:
    want = norm_name(target)
    for k, n in scope.names.items():
        if norm_name(n) == want:
            return k
    first = want.split()[0] if want else ""
    for k, n in scope.names.items():
        if first and first in norm_name(n).split():
            return k
    return None


def date_headings(scope, pid: int, back: int = 200, keep: int = 3) -> str:
    """Date headings above a table row ("Schedule of Investments ... as of March 31, 2026"), nearest first: a holdings
    table's date is often pages above the row."""
    found = []
    for q in range(pid - 1, max(0, pid - back), -1):
        t = scope.by_id[q].text if q in scope.by_id else ""
        mm = DATE_HEAD.search(t)
        if mm and (TABLE_HEAD.search(t) or len(t) < 200):
            a = max(0, mm.start() - 90)
            found.append(f"P{q}: ...{t[a:mm.end() + 10].strip()}...")
            if len(found) >= keep:
                break
    return "[dates] " + " | ".join(found) if found else ""


SCHEDULE_HEAD = re.compile(r"schedules? of investments|statements? of investments|portfolio of investments|investment portfolio|"
                           r"consolidated schedule", re.I)


def schedule_heading(scope, pid: int, back: int = 1500) -> str:
    """The heading of the schedule a row sits in, with the lines around it: a report covering several funds names the fund
    there, often pages above the row."""
    for q in range(pid - 1, max(0, pid - back), -1):
        t = scope.by_id[q].text if q in scope.by_id else ""
        if len(t) < 200 and SCHEDULE_HEAD.search(t):
            around = [scope.by_id[x].text[:110] for x in (q - 1, q, q + 1) if x in scope.by_id]
            return f"[schedule] P{q}: " + " / ".join(around)
    return ""


UNIT_HEAD = re.compile(r"\bin\s+(?:u\.?s\.?\s+)?(?:dollars\s+in\s+)?(thousands|millions)\b|\(\s*\$?\s*000'?s?(?:\s+omitted)?\s*\)|"
                       r"\$\s*000'?s\b|\b000s\b|\(\s*\$?\s*(?:in\s+)?mil(?:lion)?s?\s*\)", re.I)


def unit_headings(scope, pid: int, back: int = 300, keep: int = 2) -> str:
    """Unit headings above a table row ("(dollars in thousands)", "Value (000)"), nearest first: like the date, the unit of
    a schedule is often pages above the row."""
    found = []
    for q in range(pid, max(0, pid - back), -1):
        t = scope.by_id[q].text if q in scope.by_id else ""
        mm = UNIT_HEAD.search(t)
        if mm and (TABLE_HEAD.search(t) or len(t) < 300):
            a = max(0, mm.start() - 70)
            found.append(f"P{q}: ...{t[a:mm.end() + 10].strip()}...")
            if len(found) >= keep:
                break
    return "[units] " + " | ".join(found) if found else ""


def ncen_facts(m: dict, target: str) -> dict:
    """Form N-CEN is structured XML: read the principal transactions with the target directly (flattened to text, the
    amount of one counterparty sits next to the next counterparty's name and a model pairs them wrongly)."""
    import xml.etree.ElementTree as ET

    root = ET.parse(m["pdf"]).getroot()
    tag = lambda e: e.tag.split("}")[-1]   # noqa: E731
    period, fund = "", m["filer"]
    for e in root.iter():
        if tag(e) == "generalInfo":
            period = e.get("reportEndingPeriod", "")
        elif tag(e) == "registrantFullName" and e.text:
            fund = e.text.strip()
    facts = []
    for e in root.iter():
        if tag(e) != "principalTransaction":
            continue
        vals = {tag(x): (x.text or "").strip() for x in e}
        if norm_name(vals.get("principalName", "")) != norm_name(target):
            continue
        amt = float(vals.get("principalTotalPurchaseSale") or 0)
        facts.append({"para_no": 0, "metric": "fund_transaction", "value_number": amt, "unit": "USD", "as_of_date": period,
                      "value_text": f"{fund} reported {money(amt)} of purchases and sales with {vals['principalName']} as principal in the "
                                    f"12 months to {fmt_date(period)} (Form N-CEN, principal transactions).",
                      "evidence": f"principalName {vals['principalName']}; principalTotalPurchaseSale {vals.get('principalTotalPurchaseSale')}",
                      "confidence": 100, "check": "ok", "page": ""})
    return {"holdings": [], "facts": facts, "meta": {"parsed": "N-CEN XML", "cost_usd": 0, "unique_paragraphs": "XML"}}


def fix_columns(r: dict, para: str) -> None:
    """Keep cost and fair value on the columns the row prints them in. One money column is a value, not a cost; a figure
    with another money column before it is the fair value of a cost / value pair."""
    if r.get("position_type") in ("funded_loan", "unfunded_commitment"):
        return
    row = re.sub(r"\d{1,2}/\d{1,2}/\d{2,4}", " ", para)
    amounts = [float(x.replace(",", "")) for x in re.findall(r"(?<![\d.,])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\d%])", row)]
    cost, value = r.get("cost_usd"), r.get("fair_value_usd")
    if r.get("shares") is not None and float(r["shares"]) in amounts:   # a share count is not a money column
        amounts.remove(float(r["shares"]))
    if cost is None or float(cost) not in amounts:
        return
    if value is None and len(amounts) == 1 and r.get("shares"):
        r["cost_usd"], r["fair_value_usd"] = None, cost
    elif value is None and amounts.index(float(cost)) > 0:
        r["cost_usd"], r["fair_value_usd"] = amounts[amounts.index(float(cost)) - 1], cost
    elif value == cost and amounts.count(float(cost)) == 1:
        r["cost_usd"] = None


def extract_facts(m: dict, target: str, out: Path, model: str, refresh: bool, no_llm: bool, limiter) -> dict:
    fdir = out / "facts"
    fdir.mkdir(parents=True, exist_ok=True)
    cache = fdir / f"{m['stem']}.json"
    if s.clean_form(m["form"]).startswith("N-CEN") and m["document"].lower().endswith(".xml"):
        return ncen_facts(m, target)
    if cache.exists() and not refresh:
        return json.loads(cache.read_text(encoding="utf-8"))
    if no_llm:
        return {"holdings": [], "facts": [], "meta": {"status": "skipped (--no-llm)"}}
    scope = s.load_scope(m["pdf"], m["mentions_csv"], m["filer"])
    tid = target_id(scope, target)
    pids = sorted(p for p, comps in scope.found.items() if tid in comps) if tid else []
    short = target.split(",")[0].strip()   # one mention can stand for several rows of the same table (Series F, Series G ...)
    near = {q for pid in pids for q in range(pid - 40, pid + 41)}
    pids = sorted(set(pids) | {q for q in near if q in scope.by_id and d.has_word(scope.by_id[q].text, short)})
    blocks, seen = [], set()
    for pid in pids:
        p = scope.by_id[pid]
        key = d.norm(p.text)
        if key in seen:                       # identical rows / repeated paragraphs: ask once
            continue
        seen.add(key)
        table = d.looks_like_table(p.text) or p.text.count(" | ") >= 3
        before, after = s.context_lines(scope, pid, before=12 if table else 2, after=4 if table else 1, cut=260 if table else 450)   # a row can wrap over several lines
        dates = date_headings(scope, pid) if table else ""
        units = unit_headings(scope, pid) if table else ""
        sched = schedule_heading(scope, pid) if table else ""
        blocks.append((pid, "\n".join(x for x in (dates, units, sched, before, p.line(), after) if x)))
    chunks, cur, used = [], [], 0
    for pid, b in blocks:
        t = c.est_tokens(b)
        if cur and used + t > 6000:
            chunks.append(cur)
            cur, used = [], 0
        cur.append((pid, b))
        used += t
    if cur:
        chunks.append(cur)
    filing = s.filing_context(m["form"], m["file_date"], m["filer"], False)
    system = FACTS_SYSTEM.format(target=target, filing=filing, metrics=", ".join(METRICS), units=", ".join(UNITS))
    holdings, facts, cost, calls = [], [], 0.0, 0
    all_complete = True
    for i, ch in enumerate(chunks):
        res = c.call_llm("\n\n".join(b for _, b in ch), model, max_tokens=8000, timeout=300, limiter=limiter,
                         label=f"facts {m['stem']} {i + 1}", system=system, schema=FACTS_SCHEMA)
        got, complete = parse_facts(res.content)
        all_complete = all_complete and complete and not res.error
        cost += res.cost
        calls += 1
        texts = {pid: scope.by_id[pid].text for pid, _ in ch}
        ctx = {pid: b for pid, b in ch}
        for kind in ("holdings", "facts"):
            for r in got[kind]:
                pid = r.get("para_no")
                para, full = texts.get(pid, ""), ctx.get(pid, "")
                ev = (r.get("evidence") or "").strip()
                ok = bool(ev) and (rel.quote_in_text(ev, full) >= 0 or d.norm(ev) in d.norm(full))
                if kind == "holdings":
                    parts = [int(x.replace(",", "")) for x in re.findall(r"\d[\d,]*", r.get("security") or "")]
                    sum_ok = bool(r.get("shares")) and sum(parts) == r["shares"] and all(digits_in(x, full) for x in parts)   # classes on wrapped lines
                    nums_ok = (sum_ok or digits_in(r.get("shares"), para)) and all(digits_in(r.get(k), para) for k in ("cost_usd", "fair_value_usd", "commitment_usd"))
                    if not nums_ok and not sum_ok and all(digits_in(r.get(k), para) for k in ("cost_usd", "fair_value_usd", "commitment_usd")):
                        r["shares"] = None             # the count comes from another sentence, not from this row
                        nums_ok = True
                    r["check"] = "ok" if ok and nums_ok else ("numbers not in the row" if not nums_ok else "quote not found")
                    fix_columns(r, para)
                    if (r.get("value_scale") or 1) == 1 and re.search(r"\d\s*million\b", ev, re.I):
                        r["value_scale"] = 1000000
                    for k in ("cost_usd", "fair_value_usd", "commitment_usd"):   # the check above needs the printed figures
                        if r.get(k) is not None:
                            r[k] = r[k] * (r.get("value_scale") or 1)
                    if r.get("shares") is not None:
                        r["shares"] = r["shares"] * (r.get("shares_scale") or 1)
                else:
                    r["check"] = "ok" if ok else "quote not found"
                r["page"] = scope.by_id[pid].page if pid in scope.by_id else ""
                (holdings if kind == "holdings" else facts).append(r)
        print(f"    facts {m['form']:7} {m['file_date']} chunk {i + 1}/{len(chunks)}: {len(got['holdings'])} holdings, "
              f"{len(got['facts'])} facts, ${res.cost:.4f}{'' if complete else '  <-- INCOMPLETE ' + res.error}", flush=True)
    data = {"holdings": holdings, "facts": facts,
            "meta": {"paragraphs": len(pids), "unique_paragraphs": len(blocks), "calls": calls, "cost_usd": round(cost, 4),
                     "target_found": bool(tid), "model": model, "schema": 2}}
    if not all_complete:                       # a failed call must not be remembered as "nothing found"
        print(f"    facts {m['stem']}: incomplete, not cached", flush=True)
        return data
    cache.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return data


# ---------------------------------------------------------------------------------------------- 5. merge
LOAN = re.compile(r"\b(term loans?|loans?|lien|delayed draw|revolv\w*|notes?|sofr|libor|unitranche|bonds?)\b|\bSF\s*\+", re.I)


UNFUNDED = re.compile(r"\bunfunded\b|\bundrawn\b|\bunused commitment|\bcommitment\b", re.I)
SHARED_SPV = re.compile(r"exposure to\s+(.+?)\s+and\s+(.+?)\)", re.I)


def classify_position(h: dict) -> dict:
    """Rows from the model carry position_type; rows cached before that field get it from their text. Works for both."""
    h = dict(h)
    text = f"{h.get('security') or ''} {h.get('vehicle') or ''}"
    if not h.get("position_type"):
        all_text = f"{text} {h.get('evidence') or ''}"
        h["position_type"] = ("unfunded_commitment" if LOAN.search(all_text) and UNFUNDED.search(all_text) else
                              "funded_loan" if LOAN.search(text) else "equity")
    if h["position_type"] == "unfunded_commitment":
        if not h.get("commitment_usd"):
            h["commitment_usd"] = h.get("fair_value_usd") or h.get("cost_usd")
            h["cost_usd"] = h["fair_value_usd"] = None
        for k in ("cost_usd", "fair_value_usd"):   # the commitment repeated in a value column is not a value
            if h.get(k) == h["commitment_usd"]:
                h[k] = None
    if not h.get("shared_with"):
        mm = SHARED_SPV.search(f"{text} {h.get('evidence') or ''}")
        h["shared_with"] = mm.group(2).strip() if mm else ""
    return h


def join_split_rows(rows: list[dict]) -> list[dict]:
    """One position told in two rows of the same value and date (shares in one, cost in the other) becomes one row."""
    whole: dict = {}
    for h in rows:
        if h.get("shares") and h.get("fair_value_usd"):
            whole.setdefault((norm_name(h["holder"]), h.get("as_of_date") or "", h["fair_value_usd"]), h)
    out = []
    for h in rows:
        e = whole.get((norm_name(h["holder"]), h.get("as_of_date") or "", h.get("fair_value_usd")))
        if e is None or e is h or h.get("shares") or not h.get("as_of_date") or e.get("verified") != h.get("verified"):
            out.append(h)
            continue
        e["cost_usd"] = e.get("cost_usd") or h.get("cost_usd")
        e["acquisition_date"] = e.get("acquisition_date") or h.get("acquisition_date")
        e["sources"] += h["sources"]
    return out


# ---------------------------------------------------------------------------------------------- 5a. rule-read evidence rows
# Holdings numbers come from the table rows read by rule (evidence_tables.py + evidence_build.py), never from the model: the
# model's row gives the holder, vehicle and position type, the evidence row gives shares, cost, value, unit and dates.
CIK_TAIL = re.compile(r"\s*\(CIK\s*\d+\)\s*$", re.I)
ACC = re.compile(r"\d{10}-\d{2}-\d{6}")
FOOTNOTES = re.compile(r"\((?:[a-z]{1,2}|\d{1,2})\)")
WRAPPER = re.compile(r"\b(llc|l\.?p|spv|feeder|partners|fund|trust)\b", re.I)
AI_ONLY = "AI only — not verified"


MON = r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b\.?"
DATE_TOKEN = re.compile(rf"\d{{4}}-\d{{1,2}}-\d{{1,2}}|\d{{1,2}}/\d{{1,2}}/\d{{2,4}}|{MON}\s+\d{{1,2}},?\s+\d{{4}}|\d{{1,2}}/\d{{4}}|{MON}\s+\d{{4}}", re.I)


def ev_date(x) -> str:
    """The first date printed in a table cell (11/7/16, 2016-11-07, Sept. 30, 2016, 11/2016, November 2016; a range '11/7/16 - 4/12/18'
    gives its first date) as YYYY-MM-DD; a month alone gives its first day; "" when there is no date."""
    mm = DATE_TOKEN.search(re.sub(r"\s+", " ", str(x or "")))
    if not mm:
        return ""
    t = re.sub(rf"^({MON})", lambda g: g.group(1)[:3], mm.group(0).replace(",", " "), flags=re.I)   # 'Sept.' -> 'Sep'
    t = re.sub(r"\s+", " ", t).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%y", "%m/%d/%Y", "%b %d %Y", "%m/%Y", "%b %Y"):
        try:
            return dt.datetime.strptime(t, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def sec_key(name: str) -> str:
    return norm_name(FOOTNOTES.sub(" ", name or ""))


def same_sec(a: str, b: str) -> bool:
    """One security named in full in one table and shortened in the other ('Acme Inc. Series B' / 'Series B')."""
    return a == b or f" {a}".endswith(f" {b}") or f" {b}".endswith(f" {a}")


def join_lots(v: dict, cs: list[dict]) -> None:
    """Restricted-securities rows (cost + acquisition date) added to the schedule row of the position: one row, or several lots."""
    v.update(cost=v["cost"] if v["cost"] is not None else sum(c["cost"] for c in cs), acq=v["acq"] or min((c["acq"] for c in cs if c["acq"]), default=""),
             as_of=v["as_of"] or next((c["as_of"] for c in cs if c["as_of"]), ""), tier=max([v["tier"]] + [c["tier"] for c in cs]),
             why="; ".join(x for x in [v["why"]] + [c["why"] for c in cs] if x), raw=" || ".join([v["raw"]] + [c["raw"] for c in cs]),
             ref=" + ".join([v["ref"]] + [c["ref"] for c in cs]) + (f" ({len(cs)} lots)" if len(cs) > 1 else ""), paired=True)


def pair_rows(rows: list[dict]) -> list[dict]:
    """A cost-only row (restricted securities: cost + acquisition date) joins the value row (shares + value) of the same security in
    the same filing, when its cost and share count do not differ from the value row's. Several rows of one security (a report printed
    twice, or covering several funds) pair in table order when the counts agree; several cost rows against one value row are lots of
    it. A cost row left over beside value rows of the same security is kept for review (tier C), never counted as a second position."""
    rows = sorted(rows, key=lambda r: r["ord"])
    val = [r for r in rows if r["value"] is not None or r["commitment"] is not None]
    cost = [r for r in rows if r["value"] is None and r["commitment"] is None]
    out = list(val)
    fits = lambda v, c: ((v["cost"] is None or same_amount(v["cost"], c["cost"])) and                                  # noqa: E731
                         not (v["shares"] and c["shares"] and not same_amount(v["shares"], c["shares"])))
    for key in dict.fromkeys(sec_key(r["name"]) for r in cost):
        cs = [r for r in cost if sec_key(r["name"]) == key]
        vs = [r for r in val if not r.get("paired") and same_sec(key, sec_key(r["name"]))]
        if not vs:
            out += cs                                  # the security is only in the note: its one reading is the position
            continue
        if len(cs) != len(vs):                         # the same note row printed twice
            u = list({(c["cost"], c["acq"], c["shares"]): c for c in cs}.values())
            cs = u if len(u) == len(vs) else cs
        if len(cs) == len(vs) and all(fits(v, c) for v, c in zip(vs, cs)):
            for v, c in zip(vs, cs):
                join_lots(v, [c])
            continue
        lots = len(vs) == 1 and (not vs[0]["shares"] or not any(c["shares"] for c in cs) or same_amount(sum(c["shares"] or 0 for c in cs), vs[0]["shares"]))
        if lots and (vs[0]["cost"] is None or same_amount(vs[0]["cost"], sum(c["cost"] for c in cs))):
            join_lots(vs[0], cs)
            continue
        left = list(cs)
        for v in vs:                                   # what is left: a cost row pairs only with a value row of the same cost
            c = next((c for c in left if v["cost"] is not None and fits(v, c)), None)
            if c:
                join_lots(v, [c])
                left.remove(c)
        out += [{**c, "tier": "C", "why": "; ".join(x for x in (c["why"], "restricted-securities row not matched to a schedule row") if x)} for c in left]
    seen, keep = set(), []
    for r in out:                                      # the same position printed twice in one filing
        k = (r["shares"], r["cost"], r["value"], r["commitment"], r["acq"], r["as_of"])
        if k not in seen:
            seen.add(k)
            keep.append(r)
    return keep


def load_evidence(path: Path) -> dict | None:
    """evidence_rows.csv as positions by accession; None when the file is missing (holdings then stay as the model read them)."""
    if not path.exists():
        return None
    f = lambda x: float(x) if x not in (None, "") else None                                        # noqa: E731
    by: dict = collections.defaultdict(list)
    seen = set()
    for r in csv.DictReader(open(path, newline="", encoding="utf-8")):
        tier = (r.get("tier") or "")[:1]
        if tier not in ("A", "B", "C"):
            continue                                   # not a position
        tab, row = r.get("table") or "", r.get("row") or ""   # a table number, an iXBRL context id, 'page N' or 'text'
        x = {"accession": r["accession"], "form": r.get("form") or "", "filer": CIK_TAIL.sub("", r.get("filer") or "").strip(), "name": r.get("name") or "",
             "shares": f(r.get("qty_usd")), "cost": f(r.get("cost_usd")), "value": f(r.get("value_usd")), "commitment": f(r.get("commitment_usd")),
             "unit": f(r.get("unit_factor")) or 1, "qty_unit": f(r.get("qty_factor")) or 1, "acq": ev_date(r.get("date")),
             "as_of": ev_date(r.get("report_date")) or ev_date(r.get("as_of")), "file_date": r.get("file_date") or "", "tier": tier,
             "why": r.get("why") or "", "raw": r.get("raw") or "", "ord": tuple(int(d) for d in re.findall(r"\d+", f"{tab} {row}")) or (0,),
             "ref": f"table {tab}, row {row}" if row != "" else f"{r.get('method') or 'table'} {tab}", "url": r.get("url") or ""}
        if x["as_of"] and x["file_date"] and x["as_of"] > x["file_date"]:
            x["as_of"] = ""                            # a date after the filing is a later event, not the date of the table
        if x["acq"] and ((x["file_date"] and x["acq"] > x["file_date"]) or r.get("method") == "iXBRL tag" or x["acq"] in (x["as_of"], ev_date(r.get("as_of")))):
            x["acq"] = ""                              # an iXBRL context date or the report date is the date of the table, not of the purchase
        k = (x["accession"], tab, row, x["name"])
        if k in seen or not any(x[c] is not None for c in ("cost", "value", "commitment")):
            continue
        seen.add(k)
        by[x["accession"]].append(x)
    return {acc: pair_rows(v) for acc, v in by.items()}


def same_amount(a, b) -> bool:
    """Equal at some printed scale: the model often misses a "(000s)" heading or applies it twice."""
    if a in (None, "") or b in (None, ""):
        return False
    a, b = float(a), float(b)
    return any(abs(a * k - b) <= max(0.5, abs(b) * 1e-6) for k in (1, 1e3, 1e6, 1e-3, 1e-6))


def ev_score(h: dict, p: dict, target: str) -> int:
    """How surely a model row and an evidence position are the same position of one filing: share count and amounts, then the class."""
    if h.get("shares") and p["shares"] and not same_amount(h["shares"], p["shares"]):
        return 0                                       # another position (another fund or class)
    s = 2 * same_amount(h.get("shares"), p["shares"])
    s += 2 * any(same_amount(h.get(a), p[b]) for a, b in (("fair_value_usd", "value"), ("cost_usd", "cost"), ("commitment_usd", "commitment")))
    s += any(same_amount(h.get(a), p[b]) for a, b in (("fair_value_usd", "cost"), ("cost_usd", "value"), ("fair_value_usd", "commitment"), ("commitment_usd", "value")))
    cls = set(sec_key(p["name"]).split()) - set(norm_name(target).split())
    return s + bool(cls and cls <= set(norm_name(h.get("security") or "").split()))


def ai_values(h: dict) -> str:
    return "; ".join((f"{k} {h[k]:,.0f}" if float(h[k]).is_integer() else f"{k} {h[k]:g}") if isinstance(h.get(k), (int, float)) else f"{k} {h[k]}" for k in
                     ("shares", "cost_usd", "fair_value_usd", "commitment_usd", "pct_net_assets", "acquisition_date", "as_of_date") if h.get(k) not in (None, ""))


def from_evidence(h: dict | None, p: dict, tiers: set, target: str, missed: str = "missed by AI") -> dict:
    """One holdings row with the numbers of evidence position p; h is the model's row of the same position (None if it missed it)."""
    ok, ai = p["tier"] in tiers, h or {}
    if h is None:
        sec = re.sub(r"\s+", " ", FOOTNOTES.sub(" ", p["name"])).strip()
        rest = re.sub(re.escape(target.split(",")[0]), " ", sec, flags=re.I)
        h = {"holder": p["filer"], "vehicle": "direct" if norm_name(target.split(",")[0]) in norm_name(sec) and not WRAPPER.search(rest) else sec,
             "security": sec, "evidence": p["raw"], "confidence": 100, "check": "ok", "shared_with": "", "page": ""}
    how = "evidence tier {t}; holder and type from AI" if ai else "evidence tier {t}; " + missed
    r = {**h, "shares": p["shares"], "cost_usd": p["cost"], "fair_value_usd": p["value"], "commitment_usd": p["commitment"],
         "value_scale": int(p["unit"]), "shares_scale": int(p["qty_unit"]), "pct_net_assets": None, "acquisition_date": p["acq"] or None,
         "as_of_date": p["as_of"], "ai_as_of_date": ai.get("as_of_date") or "", "ai_values": ai_values(ai), "evidence_tier": p["tier"],
         "evidence_row": f"{p['ref']}: {p['raw']}"[:400], "verified": "yes" if ok else "no",
         "source": how.format(t=p["tier"]) if ok else f"evidence tier {p['tier']}, review ({p['why'] or 'tier not used'}); not verified"}
    r = r if r.get("position_type") else classify_position(r)
    if r["position_type"] in ("funded_loan", "unfunded_commitment"):
        r["shares"] = None                             # a loan's par is not a share count
    return r


def reconcile(ai: list[dict], pos: list[dict], target: str, tiers: set, missed: str = "missed by AI") -> list[dict]:
    """One filing: each model row takes the numbers of the evidence position it matches; positions the model missed are added;
    model rows that no evidence row confirms are kept as AI only, out of every figure."""
    # best pairs first (score, then a share-count match, then table order), each model row and each position used once
    sc = {(i, j): ev_score(h, p, target) for i, h in enumerate(ai) for j, p in enumerate(pos)}
    pairs = sorted((k for k, s in sc.items() if s >= 2), key=lambda k: (-sc[k], -same_amount(ai[k[0]].get("shares"), pos[k[1]]["shares"]), k[1], k[0]))
    row_of, used = {}, {}
    for i, j in pairs:
        if i not in row_of and j not in used:
            row_of[i] = used[j] = from_evidence(ai[i], pos[j], tiers, target)
    out = []
    for i, h in enumerate(ai):
        if i in row_of:
            out.append(row_of[i])
            continue
        js = [j for j in range(len(pos)) if sc[(i, j)] >= 2]
        if js:                                         # a second model row of a position taken (shares in one, cost in the other)
            e = used[max(js, key=lambda j: (sc[(i, j)], -j))]
            e["ai_values"] = "; ".join(x for x in (e["ai_values"], ai_values(h)) if x)
        elif h.get("cost_usd") or h.get("fair_value_usd") or h.get("commitment_usd"):
            out.append({**h, "source": AI_ONLY, "verified": "no", "ai_values": ai_values(h)})
    return out + [from_evidence(None, p, tiers, target, missed) for j, p in enumerate(pos) if j not in used]


def fix_lender_direction(rels: list[dict], target: str) -> int:
    """A loan schedule row names the company as the borrower: the company does not lend to the fund that files the schedule."""
    t, n = norm_name(target), 0
    for r in rels:
        if r["relation"] == "lends_to" and norm_name(r["from_company"]) == t and LOAN.search(r.get("evidence_quote") or ""):
            r["from_company"], r["to_company"] = r["to_company"], r["from_company"]
            r["direction_check"] = f"{r.get('direction_check') or ''}; corrected: borrower in a loan schedule".strip("; ")
            n += 1
    return n


def merge(filings: list[dict], per_filing: dict, evidence: dict | None = None, tiers: set = frozenset("AB"), target: str = "",
          add_unread: bool = True) -> tuple[list[dict], list[dict]]:
    """Holdings and facts over all filings, one row per distinct value; the latest filing is kept as the source and the
    others are counted. With evidence rows (load_evidence), every holdings number comes from them (see reconcile): one reconcile per
    accession (all its documents together); evidence positions of filings the mention step never prepared are added too
    (add_unread; off for a pilot) and counted in merge.unread."""
    hold: dict = {}
    fact: dict = {}
    groups: dict = {}
    for m in filings:                                  # an accession can hold several documents (6-K exhibits, N-CSR parts)
        groups.setdefault(m.get("accession") or (ACC.search(m["stem"]) or [m["stem"]])[0], []).append(m)
    merge.unread = [0, 0]
    if evidence is not None:
        for acc in sorted(set(evidence) - set(groups)):
            p = evidence[acc][0]
            merge.unread[0] += 1
            merge.unread[1] += len(evidence[acc])
            if add_unread:
                groups[acc] = [{"stem": acc, "accession": acc, "form": p["form"], "file_date": p["file_date"], "filer": p["filer"],
                                "sec_url": p["url"], "document": "", "unread": True}]
    for acc, ms in groups.items():
        ai = []
        for k, m in enumerate(ms):
            for h in per_filing.get(m["stem"], {}).get("holdings", []):
                if h.get("check") != "ok" or (h.get("confidence") or 0) < 50:
                    continue
                h = classify_position(h)
                if h.get("cost_usd") or h.get("fair_value_usd") or h.get("commitment_usd") or (evidence is not None and h.get("shares")):
                    ai.append({**h, "_m": k})  # a share count with no value says nothing about valuation, but can find its evidence row
        if evidence is not None:
            rows = reconcile(ai, evidence.get(acc, []), target, tiers, "filing not read by AI" if ms[0].get("unread") else "missed by AI")
        else:
            rows = [{**h, "source": "AI (no evidence file)", "verified": "yes"} for h in ai]
        for h in rows:
            m = ms[h.pop("_m", 0)]                     # a model row keeps its own document; an evidence-only row the first one
            src = {"form": m["form"], "filing_date": m["file_date"], "filer": m["filer"], "sec_url": m["sec_url"], "document": m["document"]}
            vehicle = re.sub(r"[\d¹²³⁴⁵⁶⁷⁸⁹]+$", "", norm_name(h["vehicle"])).strip()   # 'SPV³', 'SPV 2' -> 'spv'
            day = h.get("as_of_date") or acc   # undated rows of two filings can be two dates: kept apart
            if h.get("shares"):                # same position, same date, same value = one row (fill gaps from repeats)
                key = (norm_name(h["holder"]), day, h["shares"], h.get("fair_value_usd"), h["verified"])
            else:
                key = (norm_name(h["holder"]), vehicle, day, h.get("cost_usd"), h.get("fair_value_usd"), h.get("commitment_usd"), h["verified"])
            e = hold.setdefault(key, {**h, "sources": 0, "accessions": "", "seen": [], "first_filing_date": m["file_date"]})
            filled = {k: (v if v not in (None, "") else e.get(k)) for k, v in h.items()}
            e.update({**filled, **src, "sources": e["sources"] + 1, "accessions": ";".join(x for x in (e["accessions"], acc) if x)})
            e["seen"].append((m["form"], m["file_date"], m["filer"], m["sec_url"], h.get("evidence") or ""))
        for m in ms:
            data = per_filing.get(m["stem"], {})
            src = {"form": m["form"], "filing_date": m["file_date"], "filer": m["filer"], "sec_url": m["sec_url"], "document": m["document"]}
            for f in data.get("facts", []):
                if f.get("check") != "ok" or (f.get("confidence") or 0) < 50:
                    continue
                if m["form"].startswith(("485", "486")) and f["metric"] in ETF_BOILERPLATE:
                    continue                       # a series registration describes a planned product, not the company
                key = (f["metric"], f.get("as_of_date") or "", f.get("value_number"), d.norm(f.get("value_text", ""))[:60])
                e = fact.setdefault(key, {**f, "sources": 0})
                e.update({**f, **src, "sources": e["sources"] + 1})
    holdings = join_split_rows(sorted(hold.values(), key=lambda h: (norm_name(h["holder"]), h.get("as_of_date") or "", h["filing_date"])))
    for h in holdings:
        is_debt = h["position_type"] in ("funded_loan", "unfunded_commitment")   # a loan's par amount is not a share count
        h["price_per_share"] = (h["fair_value_usd"] / h["shares"]) if h.get("shares") and h.get("fair_value_usd") and not is_debt else None
        h["cost_per_share"] = (h["cost_usd"] / h["shares"]) if h.get("shares") and h.get("cost_usd") and not is_debt else None
    position_values = {round(float(v)) for h in holdings for v in (h.get("cost_usd"), h.get("fair_value_usd")) if v}

    def words(t: str) -> set:
        return {x for x in re.findall(r"[a-z0-9]+", (t or "").lower()) if len(x) > 2}

    # the same event told in other words (10-K vs 10-K/A, two paragraphs): same metric, same date (or both undated)
    # and mostly the same words -> keep the first, count the rest as repeats
    out: list[dict] = []
    for f in sorted(fact.values(), key=lambda f: (f.get("as_of_date") or "9999", f["metric"])):
        if f["metric"] == "valuation" and f.get("value_number") and round(float(f["value_number"])) in position_values:
            continue                           # a fund's position value, already in the holdings table
        wf = words(f.get("value_text", "")) | words(f.get("evidence", ""))
        dup = None
        for g in out:
            timeless = f["metric"] in ("product_business", "regulatory_status", "person_role", "investor_link")
            if g["metric"] != f["metric"] or (not timeless and (g.get("as_of_date") or "") != (f.get("as_of_date") or "")):
                continue
            wg = words(g.get("value_text", "")) | words(g.get("evidence", ""))
            if wf and wg and len(wf & wg) / len(wf | wg) >= (0.35 if f.get("as_of_date") or timeless else 0.5):
                dup = g
                break
        if dup:
            dup["sources"] += f["sources"]
        else:
            out.append(f)
    return holdings, out


def parse_split(text: str) -> tuple | None:
    """'2022-08-08:3' -> (date, 3.0)."""
    if not text:
        return None
    day, ratio = text.split(":")
    return as_date(day), float(ratio)


def adjusted(value: float, day, split) -> float:
    """A per-share value before a stock split, in today's shares."""
    return value / split[1] if split and day and day < split[0] else value


def from_rows(h: dict) -> bool:
    """Numbers taken from a rule-read evidence row (not to be corrected by rule or by the model)."""
    return (h.get("source") or "").startswith("evidence")


def price_series(holdings: list[dict], split=None) -> list[dict]:
    """One mark per (holder, vehicle, as-of date) with shares: cost basis at the acquisition date, fair value at the as-of
    date. Undated rows are left out of the series (they stay in the holdings table)."""
    pts: dict = {}
    for h in holdings:
        if not h.get("shares"):
            continue
        holder = h["holder"]
        if h.get("cost_per_share") and as_date(h.get("acquisition_date")):
            pts[(holder, "cost", h["acquisition_date"])] = {"holder": holder, "kind": "cost", "date": as_date(h["acquisition_date"]),
                                                            "value": adjusted(h["cost_per_share"], as_date(h["acquisition_date"]), split),
                                                            "filed": h["cost_per_share"], "src": h}
        if h.get("price_per_share") and as_date(h.get("as_of_date")):
            pts[(holder, "fair value", h["as_of_date"])] = {"holder": holder, "kind": "fair value", "date": as_date(h["as_of_date"]),
                                                            "value": adjusted(h["price_per_share"], as_date(h["as_of_date"]), split),
                                                            "filed": h["price_per_share"], "src": h}
    vals = sorted(p["value"] for p in pts.values())
    mid = vals[len(vals) // 2] if vals else 0
    # drop a date after the filing (a maturity, not an as-of date) and model values 10x off the median (units read wrongly); a value
    # from a rule-read evidence row has its unit printed, so a real fall or rise stays in
    keep = [p for p in pts.values() if p["date"] <= as_date(p["src"]["filing_date"]) and (from_rows(p["src"]) or mid / 10 <= p["value"] <= mid * 10)]
    return sorted(keep, key=lambda p: (p["date"], p["holder"]))


def rel_edges(rels: list[dict], target: str) -> list[dict]:
    t = norm_name(target)
    edges: dict = {}
    for r in rels:
        if t not in (norm_name(r["from_company"]), norm_name(r["to_company"])):
            continue
        k = (r["from_company"], r["relation"], r["to_company"])
        e = edges.setdefault(k, {"from": k[0], "relation": k[1], "to": k[2], "rows": 0, "filings": set(), "first": r["filing_date"],
                                 "quote": r["evidence_quote"], "checks": collections.Counter(), "form": r["form"], "sec_url": r["sec_url"]})
        e["rows"] += 1
        e["filings"].add((r["form"], r["filing_date"], r["filer"]))
        e["checks"][r.get("direction_check") or "not recorded"] += 1
        if r["filing_date"] < e["first"]:
            e.update(first=r["filing_date"], quote=r["evidence_quote"], form=r["form"], sec_url=r["sec_url"])
    return sorted(edges.values(), key=lambda e: e["first"])


def holder_edges(edges: list[dict], holdings: list[dict], target: str) -> list[dict]:
    """A holder's invests_in (equity) or lends_to (loan) link to the company is first stated by the earliest filing that reports its
    position (evidence-backed holdings rows; AI-only rows do not count) when that is earlier than its first relationship row of that
    relation; a holder with positions and no such relationship row gets one. Other relations are left as they are."""
    t = norm_name(target)
    rel_of = lambda h: "lends_to" if h["position_type"] in ("funded_loan", "unfunded_commitment") else "invests_in"   # noqa: E731
    by: dict = collections.defaultdict(list)
    for h in holdings:
        if h.get("source") == AI_ONLY:
            continue
        for k in {norm_name(h["holder"]), norm_name(CIK_TAIL.sub("", h.get("filer") or ""))} - {""}:
            by[(k, rel_of(h))] += [(*x, h) for x in h.get("seen", [])]
    done = set()
    for e in edges:
        k = (norm_name(e["from"]), e["relation"])
        if norm_name(e["to"]) != t or not by.get(k):
            continue
        done.add(k)
        for form, day, filer, url, quote, h in sorted(by[k], key=lambda x: x[1]):
            e["filings"].add((form, day, filer))
            if day < e["first"]:
                e.update(first=day, form=form, sec_url=url, quote=quote or e["quote"])
                e["checks"]["first date from a holdings row"] = 1
    for (k, rel), seen in by.items():
        hs = [x for x in seen if norm_name(x[5]["holder"]) == k]
        if (k, rel) in done or not hs or any((norm_name(CIK_TAIL.sub("", x[5].get("filer") or "")), rel) in done for x in hs):
            continue
        form, day, filer, url, quote, h = min(hs, key=lambda x: x[1])
        edges.append({"from": h["holder"], "relation": rel, "to": target, "rows": 0, "filings": {x[:3] for x in hs}, "first": day, "quote": quote,
                      "checks": collections.Counter({"from holdings rows only": len(hs)}), "form": form, "sec_url": url})
    return sorted(edges, key=lambda e: e["first"])


# ---------------------------------------------------------------------------------------------- charts (inline SVG)
INK, INK2, MUTED, GRID, AXIS, SURF = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#fcfcfb"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]   # validated categorical order (blue, orange, aqua, yellow)


def svg_bars(points: list[tuple[str, float]], title: str, w: int = 640, h: int = 220) -> str:
    if not any(v for _, v in points):
        return ""
    l, r, t, b = 56, 12, 14, 28
    top = max(v for _, v in points) * 1.15
    band = (w - l - r) / len(points)
    bw = min(24, band * 0.6)
    out = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="{esc(title)}">']
    for frac in (0, 0.5, 1):
        y = t + (h - t - b) * (1 - frac)
        out.append(f'<line x1="{l}" x2="{w - r}" y1="{y:.1f}" y2="{y:.1f}" stroke="{GRID if frac else AXIS}" stroke-width="1"/>')
        out.append(f'<text x="{l - 6}" y="{y + 4:.1f}" text-anchor="end" class="ax">{money(top * frac, "$0") if frac else "$0"}</text>')
    for i, (lab, v) in enumerate(points):
        x = l + band * i + (band - bw) / 2
        out.append(f'<text x="{x + bw / 2:.1f}" y="{h - 10}" text-anchor="middle" class="ax">{esc(lab)}</text>')
        if not v:
            continue
        bh = (h - t - b) * v / top
        y0 = h - b
        rr = min(4, bh)
        out.append(f'<path d="M{x:.1f},{y0} V{y0 - bh + rr:.1f} Q{x:.1f},{y0 - bh:.1f} {x + rr:.1f},{y0 - bh:.1f} '
                   f'H{x + bw - rr:.1f} Q{x + bw:.1f},{y0 - bh:.1f} {x + bw:.1f},{y0 - bh + rr:.1f} V{y0} Z" fill="{SERIES[0]}">'
                   f'<title>{esc(lab)}: {money(v)}</title></path>')
        out.append(f'<text x="{x + bw / 2:.1f}" y="{y0 - bh - 5:.1f}" text-anchor="middle" class="val">{money(v)}</text>')
    out.append("</svg>")
    return "".join(out)


def svg_marks(pts: list[dict], title: str, w: int = 640, h: int = 240) -> str:
    """Price per share over time, one series per holder (cost basis = open dot, fair value = filled dot)."""
    if len(pts) < 1:
        return ""
    l, r, t, b = 56, 150, 14, 30
    d0, d1 = min(p["date"] for p in pts), max(p["date"] for p in pts)
    span = max((d1 - d0).days, 30)
    top = max(p["value"] for p in pts) * 1.15
    X = lambda dd: l + (w - l - r) * (dd - d0).days / span   # noqa: E731
    Y = lambda v: t + (h - t - b) * (1 - v / top)            # noqa: E731
    out = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="{esc(title)}">']
    for frac in (0, 0.5, 1):
        y = Y(top * frac)
        out.append(f'<line x1="{l}" x2="{w - r}" y1="{y:.1f}" y2="{y:.1f}" stroke="{GRID if frac else AXIS}" stroke-width="1"/>')
        out.append(f'<text x="{l - 6}" y="{y + 4:.1f}" text-anchor="end" class="ax">{model_report.pps(top * frac)}</text>')
    months = sorted({p["date"].replace(day=1) for p in pts})
    for dd in months[::max(1, len(months) // 8)]:
        if d0 <= dd <= d1 or len(pts) == 1:
            out.append(f'<text x="{X(dd):.1f}" y="{h - 10}" text-anchor="middle" class="ax">{dd.strftime("%b %y")}</text>')
    holders = list(dict.fromkeys(p["holder"] for p in pts))
    for i, hd in enumerate(holders[:4]):
        col = SERIES[i]
        ser = [p for p in pts if p["holder"] == hd]
        if len(ser) > 1:
            path = " ".join(f"{'M' if j == 0 else 'L'}{X(p['date']):.1f},{Y(p['value']):.1f}" for j, p in enumerate(ser))
            out.append(f'<path d="{path}" fill="none" stroke="{col}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
        for p in ser:
            fill = col if p["kind"] == "fair value" else SURF
            out.append(f'<circle cx="{X(p["date"]):.1f}" cy="{Y(p["value"]):.1f}" r="4.5" fill="{fill}" stroke="{col}" stroke-width="2">'
                       f'<title>{esc(hd)} · {p["kind"]} · {fmt_date(p["date"])}: {model_report.pps(p["value"])} per share</title></circle>')
        last = ser[-1]
        out.append(f'<text x="{X(last["date"]) + 9:.1f}" y="{Y(last["value"]) + 4:.1f}" class="val">{model_report.pps(last["value"])}</text>')
        out.append(f'<circle cx="{w - r + 16}" cy="{t + 8 + i * 18}" r="4.5" fill="{col}"/>'
                   f'<text x="{w - r + 26}" y="{t + 12 + i * 18}" class="ax">{esc(hd[:22])}</text>')
    out.append("</svg>")
    return "".join(out)


# ---------------------------------------------------------------------------------------------- 6. report
CSS = """
@page { size: A4; margin: 16mm 14mm 16mm 14mm; }
:root { --ink:#0b0b0b; --ink2:#52514e; --muted:#898781; --grid:#e1e0d9; --surf:#fcfcfb; --plane:#f9f9f7; --accent:#2a78d6; }
body { font-family: -apple-system, "Helvetica Neue", Arial, sans-serif; color: var(--ink); background: #fff; font-size: 10.5px; line-height: 1.45; }
h1 { font-size: 26px; margin: 0 0 4px; } h2 { font-size: 16px; margin: 22px 0 6px; border-bottom: 1px solid var(--grid); padding-bottom: 3px; }
h3 { font-size: 12px; margin: 14px 0 4px; } .kicker { letter-spacing: .3em; color: var(--muted); font-size: 10px; }
.cover { page-break-after: always; min-height: 250mm; } .page { page-break-before: always; }
table { border-collapse: collapse; width: 100%; margin: 6px 0 10px; font-size: 9.5px; }
th, td { text-align: left; padding: 3px 5px; border-bottom: 1px solid var(--grid); vertical-align: top; }
th { color: var(--ink2); font-weight: 600; } td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.note { color: var(--ink2); font-size: 9.5px; } .muted { color: var(--muted); }
.ph { display: inline-block; border: 1px dashed #eb6834; color: #9a3a12; background: #fff6f1; border-radius: 3px; padding: 0 4px; font-size: 9px; white-space: nowrap; }
.card { border: 1px solid var(--grid); border-radius: 6px; padding: 7px 10px; margin: 8px 0; page-break-inside: avoid; background: var(--plane); }
.card h3 { margin: 0 0 4px; } .card ul { margin: 2px 0 4px 16px; padding: 0; } .model { margin-top: 4px; color: var(--ink2); }
.box { border-left: 3px solid #eb6834; background: #fff6f1; padding: 6px 10px; margin: 8px 0; }
.chart { width: 100%; max-width: 640px; display: block; margin: 6px 0 2px; } .chart .ax { font-size: 10px; fill: #898781; } .chart .val { font-size: 10px; fill: #52514e; }
.cap { color: var(--muted); font-size: 9px; margin-bottom: 10px; } .grade { font-weight: 700; }
a { color: var(--accent); text-decoration: none; } .src { font-size: 8.5px; color: var(--ink2); word-break: break-all; }
"""


def load_nport(path: str, split) -> list[dict]:
    """Output of nport.py build: every position as filed, plus the per-share value in today's shares."""
    rows = list(csv.DictReader(open(path, newline="", encoding="utf-8")))
    for r in rows:
        r["date"] = as_date(r["report_date"])
        r["fund"] = r["series_id"] or r["cik"]
        r["value"] = float(r["value_usd"] or 0)
        r["filed"] = float(r["filed_price_per_share"]) if r["filed_price_per_share"] else None
        r["adj_price_per_share"] = round(adjusted(r["filed"], r["date"], split), 4) if r["filed"] else ""
        r["q"] = f"{r['date'].year} Q{(r['date'].month - 1) // 3 + 1}"
    return rows


def last_per_fund(rows: list[dict]) -> list[dict]:
    """Within a quarter each fund counts once, with its last report date."""
    last: dict = {}
    for r in rows:
        k = (r["q"], r["fund"])
        last[k] = max(last.get(k, r["date"]), r["date"])
    return [r for r in rows if r["date"] == last[(r["q"], r["fund"])]]


def nport_quarters(rows: list[dict], split) -> list[dict]:
    eq = [r for r in last_per_fund(rows) if r["position_type"] == "equity" and r["filed"]]
    by: dict = collections.defaultdict(list)
    for r in eq:
        by[r["q"]].append(r)
    out = []
    for q in sorted(by):
        x = by[q]
        adj = [float(r["adj_price_per_share"]) for r in x]
        out.append({"q": q, "end": max(r["date"] for r in x), "funds": len({r["fund"] for r in x}), "rows": len(x),
                    "value": sum(r["value"] for r in x), "median_filed": statistics.median(r["filed"] for r in x),
                    "median_adj": statistics.median(adj), "low": min(adj), "high": max(adj)})
    return out


def full_quarter(nq: list[dict]) -> dict:
    """The latest quarter most funds have filed for."""
    return nq[-2] if len(nq) > 1 and nq[-1]["funds"] < 0.7 * nq[-2]["funds"] else nq[-1]


def nport_section(w, rows: list[dict], nq: list[dict], split, short: str) -> None:
    pps = model_report.pps
    adj_txt = f"adjusted for the {split[1]:g}-for-1 split of {fmt_date(split[0])}" if split else "as filed"
    full = full_quarter(nq)
    w(f'<h3>Quarterly fair-value marks (Form N-PORT)</h3><p class="note">{len(rows):,} positions from {len({r["accession"] for r in rows}):,} filings of '
      f'{len({r["fund"] for r in rows})} funds, {fmt_date(min(r["date"] for r in rows))} to {fmt_date(max(r["date"] for r in rows))}. '
      f'Values and share counts are as filed; per-share values are value / shares, {adj_txt}. All classes share one median, so a move can come from the '
      'mix of classes as well as from the marks. Funds report on different month ends. A later amendment replaces the earlier filing.</p>')
    pts = [{"holder": "Median, all funds", "kind": "fair value", "date": q["end"], "value": q["median_adj"]} for q in nq]
    w(svg_marks(pts, "Median fund mark per share by quarter") + f'<div class="cap">Median fair value per share across funds, {adj_txt}.{" The last point is part of a quarter." if nq[-1] is not full else ""} Source: Form N-PORT.</div>')
    w('<table><tr><th>Quarter</th><th class="n">Funds</th><th class="n">Value held</th><th class="n">Median as filed</th><th class="n">Median adjusted</th>'
      '<th class="n">Low</th><th class="n">High</th></tr>')
    for q in nq:
        w(f"<tr><td>{q['q']}{' (part)' if q is nq[-1] and q is not full else ''}</td><td class='n'>{q['funds']}</td><td class='n'>{money(q['value'])}</td>"
          f"<td class='n'>{pps(q['median_filed'])}</td><td class='n'>{pps(q['median_adj'])}</td><td class='n'>{pps(q['low'])}</td><td class='n'>{pps(q['high'])}</td></tr>")
    w("</table>")
    now = [r for r in last_per_fund(rows) if r["q"] == full["q"]]
    eq = [r for r in now if r["position_type"] == "equity" and r["filed"]]
    w(f'<h3>Share classes in {full["q"]} (N-PORT)</h3><table><tr><th>Class</th><th class="n">Funds</th><th class="n">Value held</th>'
      '<th class="n">Median per share</th><th class="n">Low</th><th class="n">High</th></tr>')
    classes: dict = collections.defaultdict(list)
    for r in eq:
        classes[r["share_class"]].append(r)
    for c in sorted(classes):
        x = classes[c]
        pr = [r["filed"] for r in x]
        w(f"<tr><td>{esc(c)}</td><td class='n'>{len({r['fund'] for r in x})}</td><td class='n'>{money(sum(r['value'] for r in x))}</td>"
          f"<td class='n'>{pps(statistics.median(pr))}</td><td class='n'>{pps(min(pr))}</td><td class='n'>{pps(max(pr))}</td></tr>")
    w("</table>")
    houses: dict = collections.defaultdict(list)
    for r in eq:
        houses[r["fund_house"]].append(r)
    top = sorted(houses.items(), key=lambda kv: -sum(r["value"] for r in kv[1]))[:10]
    total = sum(r["value"] for r in eq) or 1
    w(f'<h3>Largest fund groups in {full["q"]} (N-PORT)</h3><table><tr><th>Fund group</th><th class="n">Funds</th><th class="n">Value held</th>'
      '<th class="n">Share of N-PORT total</th><th>Largest fund (filing)</th></tr>')
    for house, x in top:
        v = sum(r["value"] for r in x)
        funds: dict = collections.Counter()
        for r in x:
            funds[r["series"]] += r["value"]
        big = funds.most_common(1)[0][0]
        src = next(r for r in x if r["series"] == big)
        w(f"<tr><td>{esc(house)}</td><td class='n'>{len(funds)}</td><td class='n'>{money(v)}</td><td class='n'>{v / total:.1%}</td>"
          f"<td><a href='{esc(src['sec_url'])}'>{esc(big)}</a></td></tr>")
    w("</table>")
    loans = [r for r in last_per_fund(rows) if r["position_type"] == "funded_loan"]
    if loans:
        lq: dict = collections.defaultdict(list)
        for r in loans:
            lq[r["q"]].append(r)
        w('<h3>Loans reported on N-PORT</h3><table><tr><th>Quarter</th><th class="n">Funds</th><th class="n">Positions</th><th class="n">Principal</th><th class="n">Value</th></tr>')
        for q in sorted(lq):
            x = lq[q]
            w(f"<tr><td>{q}</td><td class='n'>{len({r['fund'] for r in x})}</td><td class='n'>{len(x)}</td>"
              f"<td class='n'>{money(sum(float(r['par_or_units'] or 0) for r in x))}</td><td class='n'>{money(sum(r['value'] for r in x))}</td></tr>")
        w("</table>")
    rest = [r for r in rows if r["position_type"] in ("spv_interest", "other")]
    if rest:
        w(f'<p class="note">{len(rest)} further positions are left out of these tables: funds that hold {esc(short)} through a vehicle, or rows without a '
          'usable share class. They are in nport_holdings.csv.</p>')


ACQ_ROW = re.compile(r"\|\s*(\d{1,2}/\d{1,2}/\d{2,4})\s*\|\s*\$?\s*([\d,]+)\s*$")
ACQ_KEY = re.compile(r"Series\s+(\S+?),?\s*\(Acquired\s+(\d{1,2}/\d{1,2}/\d{2,4}),\s*cost\s*\$\s*([\d,]+)\)", re.I)


def filing_text(document: str) -> str:
    """The filing as plain text, for re-reading a table row."""
    raw = (APP / "data" / "pdfs" / document).read_text(encoding="utf-8", errors="ignore")
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw)).replace("\u200c", ""))


def slash_date(text: str):
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def repair_holdings(holdings: list[dict], nport: list[dict], split) -> collections.Counter:
    """Corrections that need no model call. Each changed row says so in the repair column of holdings.csv.
    1. A restricted-securities table prints name | acquisition date | acquisition cost: that amount is a cost, not a value.
    2. Where a table prints the name first and the numbers after it, the numbers just before the name belong to the row
       above. The row is read again from the filing, by its series, acquisition date and cost.
    3. A per-share value about 1000x above or below the N-PORT mark of that date means a scale was missed or applied to the wrong column.
    Also a scale applied twice (the stored value is the printed figure x scale x scale).
    4. A per-share value still more than 3x from the N-PORT mark is not used; the row stays in holdings.csv."""
    tally: collections.Counter = collections.Counter()
    texts: dict = {}
    for h in holdings:
        h["repair"] = ""
        if from_rows(h):
            continue
        ev = (h.get("evidence") or "").replace("\u200c", "").strip()
        scale = h.get("value_scale") or 1
        printed = {float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", ev)}
        for k in ("fair_value_usd", "cost_usd"):       # the scale applied twice: the printed figure is the stored value / scale^2
            if scale > 1 and h.get(k) and h[k] / scale not in printed and h[k] / scale ** 2 in printed:
                h[k] /= scale
                tally["double_scale"] += 0 if h["repair"] else 1
                h["repair"] = "scale applied twice"
        if h["position_type"] in ("funded_loan", "unfunded_commitment"):
            continue
        acq = ACQ_ROW.search(ev)
        if acq and h.get("fair_value_usd"):
            amount = float(acq[2].replace(",", ""))
            if amount in (h["fair_value_usd"], h["fair_value_usd"] / scale):
                h.update(cost_usd=amount, fair_value_usd=None, acquisition_date=slash_date(acq[1]) or h.get("acquisition_date"),
                         shares=None if h.get("shares") == amount else h.get("shares"),
                         repair=(h["repair"] + "; " if h["repair"] else "") + "acquisition cost read as fair value")
                tally["acquisition_cost"] += 1
                continue
        key = ACQ_KEY.search(ev)
        if key and h.get("document"):
            if h["document"] not in texts:
                texts[h["document"]] = filing_text(h["document"])
            found = set(re.findall(rf"Series\s+{re.escape(key[1])},?\s*\(Acquired\s+{re.escape(key[2])},\s*cost\s*\$\s*{re.escape(key[3])}\)"
                                   r"\s*(?:\([a-z0-9]+\)\s*)*[.\s]*(\d[\d,]*)\s+\$?\s*(\d[\d,]*)", texts[h["document"]]))
            if len(found) == 1:
                sh, val = [float(x.replace(",", "")) for x in found.pop()]
                sh, val = sh * (h.get("shares_scale") or 1), val * scale
                if (sh, val) != (h.get("shares"), h.get("fair_value_usd")):
                    h.update(shares=sh, fair_value_usd=val, repair=(h["repair"] + "; " if h["repair"] else "") + "numbers of the row above; read again from the filing")
                    tally["row_offset"] += 1
    quarters = [(q["end"], q["median_adj"]) for q in nport_quarters(nport, split)] if nport else []

    def per_share(h: dict) -> None:
        h["price_per_share"] = h["fair_value_usd"] / h["shares"] if h.get("shares") and h.get("fair_value_usd") else None
        h["cost_per_share"] = h["cost_usd"] / h["shares"] if h.get("shares") and h.get("cost_usd") else None

    dollar_filers: set = set()
    for h in holdings:
        if h["position_type"] in ("funded_loan", "unfunded_commitment") or from_rows(h):
            continue
        per_share(h)
        day = as_date(h.get("as_of_date")) or as_date(h.get("filing_date"))
        near = min(quarters, key=lambda q: abs((q[0] - day).days), default=None) if day else None
        if not h.get("price_per_share") or not near or abs((near[0] - day).days) > 200:
            continue
        ratio = adjusted(h["price_per_share"], day, split) / near[1]
        scale = h.get("value_scale") or 1
        if ratio >= 30 and scale > 1 and 0.2 <= ratio / scale <= 10:
            if h.get("pct_net_assets"):        # the fund's own weight supports the value, so the share count is what was short
                h["shares"] *= scale
            else:
                h["fair_value_usd"] /= scale
                dollar_filers.add((h["filer"], h["fair_value_usd"]))
                h["cost_usd"] = h["cost_usd"] / scale if h.get("cost_usd") else h.get("cost_usd")
                h["value_scale"] = 1
            h["repair"] = (h["repair"] + "; " if h["repair"] else "") + "scale corrected against the N-PORT mark"
            tally["scale"] += 1
            per_share(h)
            ratio = adjusted(h["price_per_share"], day, split) / near[1]
        elif 0.5 <= ratio * 1000 <= 2 and scale == 1:   # a table in thousands read as dollars (a wider band hits misread columns)
            h["fair_value_usd"] *= 1000
            h["cost_usd"] = h["cost_usd"] * 1000 if h.get("cost_usd") else h.get("cost_usd")
            h["value_scale"] = 1000
            h["repair"] = (h["repair"] + "; " if h["repair"] else "") + "scale corrected against the N-PORT mark"
            tally["scale"] += 1
            per_share(h)
            ratio *= 1000
        if not 1 / 3 <= ratio <= 3:
            h["price_per_share"] = h["cost_per_share"] = None
            h["repair"] = (h["repair"] + "; " if h["repair"] else "") + f"per-share value not used: {ratio:.3g}x the N-PORT mark"
            tally["per_share_hidden"] += 1
    for h in holdings:                         # the same printed figure as a row corrected above, so the same unit
        if (h.get("value_scale") or 1) > 1 and h.get("cost_usd") and not h.get("fair_value_usd") and not h.get("shares") and not from_rows(h) \
                and (h["filer"], h["cost_usd"] / h["value_scale"]) in dollar_filers:
            h["cost_usd"] /= h["value_scale"]
            h["value_scale"] = 1
            h["repair"] = (h["repair"] + "; " if h["repair"] else "") + "scale corrected: same printed figure as a corrected row"
            tally["scale"] += 1
    return tally


def load_employee(folder: str, short: str) -> dict:
    """Department of Labor Form 5500 figures by plan year: 401(k) plan and health and welfare plan of one company."""
    out: dict = collections.defaultdict(dict)
    with open(Path(folder) / "retirement_401k_by_year.csv", newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["company"].lower() == short.lower() and r["active_participants_eoy"]:
                out[int(r["plan_year"])].update(k401=int(r["active_participants_eoy"]), deferrals=float(r["employee_contributions"] or 0),
                                                employer=float(r["employer_contributions"] or 0), net_assets=float(r["net_assets_eoy"] or 0),
                                                form=r["form"], ein=r["ein"])
    with open(Path(folder) / "welfare_plans_by_year.csv", newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["company"].lower() == short.lower() and r["plan_kind"] == "Health and welfare" and r["active_participants_eoy"]:
                out[int(r["plan_year"])].setdefault("health", int(r["active_participants_eoy"]))
    return dict(out)


def nport_check(holdings: list[dict], rows: list[dict]) -> collections.Counter:
    """Each dated equity row from an annual or half-year report against the N-PORT rows of the same fund and date. The model
    read the report; N-PORT is the fund's own structured filing, so this is an outside check on the extraction."""
    skip = {"fund", "the", "inc", "trust", "portfolio", "series", "funds", "class", "i", "ii", "llc", "corp", "and", "of"}
    words = lambda x: {w for w in re.findall(r"[a-z0-9]+", (x or "").lower()) if w not in skip}   # noqa: E731
    by_day: dict = collections.defaultdict(list)
    for r in rows:
        if r["position_type"] == "equity":
            by_day[r["report_date"]].append(r)
    near = lambda x, y: abs(x - y) <= max(1.5, abs(y) * 0.002)                                     # noqa: E731
    tally: collections.Counter = collections.Counter()
    for h in holdings:
        h["nport_check"] = ""
        if h["position_type"] != "equity" or not h.get("fair_value_usd") or not h.get("as_of_date"):
            continue
        name = words(h["holder"])
        day = by_day.get(str(h["as_of_date"]), [])
        same = [r for r in day if name and len(name & words(f"{r['series']} {r['registrant']}")) >= max(1, len(name) - 1)]
        values = [r["value"] for r in same]
        fv = float(h["fair_value_usd"])
        family = next(iter(re.findall(r"[a-z0-9]+", (h.get("filer") or "").lower())), "")
        tally["same_fund_rows"] += 1 if same else 0
        if any(near(fv, v) for v in values + ([sum(values)] if values else [])):
            h["nport_check"], key = "value matches", "match"
        elif fv >= 50000 and any(near(fv, r["value"]) and family in words(f"{r['series']} {r['registrant']}") for r in day):
            # a trust files one report for many funds, so the name often differs. Same family and not a tiny amount, which would match by chance.
            h["nport_check"], key = "value matches another fund's N-PORT row of that date", "other_fund"
        elif same:
            h["nport_check"], key = "value differs: " + "; ".join(f"{r['share_class']} {money(r['value'])}" for r in same[:4]), "differs"
        else:
            h["nport_check"], key = "no N-PORT row for this fund and date", "none"
        tally[key] += 1
    return tally


def build_html(a, fd: dict, filings: list[dict], holdings: list[dict], facts: list[dict], edges: list[dict],
               per_filing: dict) -> tuple[str, list[str]]:
    P = Placeholders()
    co = fd["company"]
    name = a.name or co["company_name"]
    short = getattr(a, "short", "") or short_name(name, {})
    cik_int = int(co["cik"])
    own = lambda m: str(int(m["filer_cik"])) == str(cik_int) if str(m.get("filer_cik") or "").isdigit() else norm_name(m["filer"]) == norm_name(name)   # noqa: E731
    others = [m for m in filings if not own(m)]       # filings by other companies and funds (not the company's own Form D)
    allh, holdings = holdings, [h for h in holdings if h.get("verified", "yes") == "yes"]   # model-only and tier-C numbers feed no figure
    ev = getattr(a, "evidence", None)
    unread = getattr(a, "unread", None) or [0, 0]
    n_ai, n_rev = sum(h.get("source") == AI_ONLY for h in allh), sum(from_rows(h) and h.get("verified") == "no" for h in allh)
    ev_note = (" No rule-read evidence file (evidence_rows.csv) was found, so holdings amounts are as the language model read them and are not verified." if ev is None else
               f" Shares, cost, value and dates come from the table rows read by rule (evidence_rows.csv, tier {'/'.join(sorted(a.tiers))}); "
               f"{n_ai} row(s) only the language model read and {n_rev} evidence row(s) marked for review are left out of every figure and chart (holdings.csv, verified = no)."
               + (f" {unread[1]} evidence position(s) in {unread[0]} filing(s) the mention step did not prepare are "
                  + ("added with the filer as holder." if not getattr(a, "only_stems", "") else "left out (pilot run).") if unread[0] else ""))
    today = dt.date.today()
    first_year = fd["year_of_inc"] or min(as_date(o["sale_date"] or o["filing_date"]).year for o in fd["offerings"])
    last_hist = max([as_date(fd["db_until"]).year] + [as_date(m["file_date"]).year for m in filings])
    years = list(range(first_year, last_hist + 1))
    fyears = list(range(last_hist + 1, last_hist + 1 + FORECAST_YEARS))
    offs = fd["offerings"]

    def sale_year(o) -> int:
        return (as_date(o["sale_date"]) or as_date(o["filing_date"])).year

    raised = collections.Counter()
    for o in offs:
        raised[sale_year(o)] += float(o["total_amount_sold"] or 0)
    any_debt = any(o["is_debt"] for o in offs)
    split = parse_split(getattr(a, "split", ""))
    split_txt = f"{split[1]:g}-for-1 split of {fmt_date(split[0])}" if split else ""
    nq = nport_quarters(a.nport, split) if getattr(a, "nport", None) else []
    pts = price_series(holdings, split)
    marks_by_year: dict = {}
    for p in pts:
        if p["kind"] == "fair value":
            marks_by_year[p["date"].year] = p          # latest mark of the year (pts are sorted by date)
    for q in nq:                                       # the median of all funds beats one fund's mark
        marks_by_year[q["end"].year] = {"value": q["median_adj"]}
    facts_by_year: dict = collections.defaultdict(list)
    for f in facts:
        y = as_date(f.get("as_of_date"))
        facts_by_year[y.year if y else None].append(f)
    total_raised = float(co["total_raised"] or 0)
    spv_total = sum(float(x["total_raised"] or 0) for x in fd["spvs"])
    spv_inv = sum(int(x["total_investors"] or 0) for x in fd["spvs"])
    volume = [f for f in facts if f["metric"] == "trading_volume"]
    rev_facts = [f for f in facts if f["metric"] in ("revenue", "revenue_run_rate")]
    emp_facts = [f for f in facts if f["metric"] == "employees"]
    plans = getattr(a, "employee", None) or {}
    fm, cm = getattr(a, "fin", None), getattr(a, "cred", None)
    fm_y = fm["fy"] - 1 if fm else None                       # the calendar year holding most of the modelled fiscal year
    mu, pps = model_report.usd, model_report.pps
    fc_rows = [r for r in model_report.rows(a.model_out, "credit_projections.csv") if r["scenario"] == "Base"] if fm else []
    us_staff = lambda y: plans.get(y, {}).get("health") or plans.get(y, {}).get("k401")   # noqa: E731
    plan_last = max(plans) if plans else None
    statements = sorted(volume + rev_facts, key=lambda f: str(f.get("as_of_date") or ""))
    rev_ranges = sorted({f["revenue_range"] for f in fd["filings"] if f["revenue_range"]})

    def grade(y: int) -> str:
        has_d = raised.get(y, 0) > 0
        has_m = y in marks_by_year
        if has_d and has_m:
            return "B"
        if has_d or has_m:
            return "C"
        if any(as_date(m["file_date"]).year == y for m in filings) or facts_by_year.get(y):
            return "D"
        return "–"

    H: list[str] = []
    w = H.append
    # ---------------- cover
    w(f"""<div class="cover"><div class="kicker">P U B L I C &nbsp; R E C O R D &nbsp; R E S E A R C H</div><br><br>
<div class="muted">FINANCIAL MODEL · UPDATED {today.strftime('%m/%d/%Y')}</div>
<h1>{esc(short)}: financials year by year, {first_year}-{fyears[-1]}</h1><br>
<h3>SCOPE</h3><p>Funding, ownership, valuation marks, relationships and legal record of {esc(name)} (SEC CIK {esc(co['cik'])}) for
every year from incorporation in {first_year} to {last_hist}, plus the frame for {fyears[0]}-{fyears[-1]} forecasts. Built from the SEC
Form D database ({len(fd['filings'])} Form D filings by the company, {len(fd['spvs'])} SPVs) and {len(others)} SEC filings by other companies and
funds that name {esc(short)}{", plus Department of Labor Form 5500 benefit-plan filings" if plans else ""}. {"Revenue, staff, cost, credit and forecast figures are model estimates built from these records and labelled as such; figures that remain unknown" if fm else "Revenue, " + ("global " if plans else "") + "headcount, cost and forecast figures are not in these sources: they"} are shown as placeholders
(<span class="ph">R1</span> ...) and the forms needed to fill each one are listed in the final sections.</p>
<h3>DISCLAIMERS</h3><p>{esc(name)} is private and publishes no financial statements. Form D amounts are what the company reported to the
SEC, not certified cash received. Fund fair values are the funds' own estimates. Relationships and facts were read from the filings by a
language model (GPT-4.1); every row keeps a verbatim quote that was checked against the filing text.{" Holdings amounts come from table rows read by rule; amounts only the model read are not used." if ev is not None else ev_note}</p>
<p>Not investment advice. Not affiliated with, endorsed by, or prepared with information from {esc(name.rstrip("."))}. Inputs as of
{fmt_date(fd['db_until'])} (Form D database) and {fmt_date(max(m['file_date'] for m in filings))} (latest filing).</p>
<p class="muted">Sources: public records - see final section</p></div>""")

    # ---------------- summary
    only_eq = sum(float(o["total_amount_sold"] or 0) for o in offs if o["is_equity"] and not o["is_debt"])
    only_debt = sum(float(o["total_amount_sold"] or 0) for o in offs if o["is_debt"] and not o["is_equity"])
    both = sum(float(o["total_amount_sold"] or 0) for o in offs if o["is_debt"] and o["is_equity"])
    debt_split = (f" ({money(only_eq)} equity only, {money(only_debt)} debt only, {money(both)} marked as both, as ticked on the form)"
                  if any_debt else "")
    first_mark, last_mark = (pts[0], pts[-1]) if pts else (None, None)
    w(f"""<h2>{esc(short)} raised {money(total_raised)} in {len(offs)} Form D offerings, {first_year}-{last_hist}</h2>
<p><b>TL;DR:</b> {esc(name)}, incorporated in {esc(co['jurisdiction_of_inc'] or '').title()} in {first_year} and based in
{esc((co['city'] or '').title())}, {esc(co['state'] or '')}, has filed {len(fd['filings'])} Form D notices. They report
{money(total_raised)} of {'equity and debt' if any_debt else 'equity'} sold to {int(co['total_investors'] or 0)} investors in {len(offs)} offerings,
the largest {money(co['largest_offering'])}{debt_split}; {'the revenue range was "' + esc(rev_ranges[0]) + '" on every filing' if len(rev_ranges) == 1 else 'revenue ranges given: ' + esc(', '.join(rev_ranges) or 'none')}.
{len(fd['spvs'])} special-purpose vehicles named after {esc(short)} raised a further {money(spv_total)} from {spv_inv} investors to buy its shares.
""" + (f"On Form N-PORT, funds marked {esc(short)} at a median {pps(nq[0]['median_adj'])} per share in {nq[0]['q']} and "
       f"{pps(full_quarter(nq)['median_adj'])} in {full_quarter(nq)['q']}{', adjusted for the ' + split_txt if split else ''}; "
       f"{full_quarter(nq)['funds']} funds reported {money(full_quarter(nq)['value'])} of its shares in that quarter. " if nq else
       f"Fund holdings put a value of {pps(first_mark['value'])} per share on {esc(short)} on {fmt_date(first_mark['date'])} "
       f"({esc(first_mark['holder'])}, {first_mark['kind']}) and {pps(last_mark['value'])} on {fmt_date(last_mark['date'])} "
       f"({esc(last_mark['holder'])}). " if pts else "") +
      (f"Its U.S. benefit plans show {us_staff(plan_last):,} active participants at the end of {plan_last}, not total headcount. " if plans else "") +
      (f"Model estimates for FY{fm['fy']}: revenue {mu(fm['rev'])}, EBITDA proxy {mu(fm['ebitda'])}, FCF proxy {mu(fm['fcf'])}, about {round(fm['hc']):,} staff on a {model_report.pct(a.fin['cfg']['us_share'], 0)} U.S. share. "
       f"The model's synthetic rating is {cm['rating']}, with a {model_report.pct(cm['pds'][0]['r'], 2)} one-year default probability. These are model outputs, not company figures.</p>" if fm else
       f"Revenue {P('R1')}, {'global ' if plans else ''}headcount {P('R3')}, EBITDA and cash {P('R6')} and the company valuation {P('R4')} are not in the "
       "sources used; the forms that would give them are listed at the end.</p>"))
    w(svg_bars([(str(y), raised.get(y, 0)) for y in years], "Capital raised by year"))
    w(f'<div class="cap">Capital raised per year (Form D amount sold, by date of first sale). Source: Form D database.</div>')

    # ---------------- every year at a glance
    w('<div class="page"></div><h2>Every year at a glance</h2>')
    w(f'<p class="note">Calendar years ({"the models use fiscal years" if fm else P("R13", "fiscal year end unknown")}). The grade says how much of the year is pinned by records: '
      'B = Form D offering and a fund valuation mark; C = one of the two; D = only mentions in other filings; – = no record; '
      'F = forecast. A grade about revenue (A = company-reported revenue) needs R1. All money is nominal U.S. dollars.</p>')
    w('<table><tr><th>Year</th><th>Grade</th><th>Revenue</th><th>Range</th><th>Growth</th><th>Run-rate at year end</th>'
      '<th>Avg employees</th><th>EBITDA proxy</th><th>FCF proxy</th><th class="n">Equity + debt raised</th><th>Valuation</th></tr>')
    for y in years + fyears:
        fc = y in fyears
        mk = marks_by_year.get(y)
        val = (f"{pps(mk['value'])}/share (fund mark{', split-adjusted' if split else ''}) {P('R4')}" if mk else P("R4"))
        rr = raised.get(y, 0)
        fc_row = next((r for r in fc_rows if r["fiscal_year"] == f"FY{y}"), None) if fc else None
        if fc_row:
            cells = (f"<td>{mu(fc_row['revenue_usd'])} (base)</td><td>{P('R10')}</td><td>{model_report.pct(fc_row['revenue_growth'], 0)}</td><td>{P('R10')}</td>"
                     f"<td>{P('R3')}</td><td>{mu(fc_row['ebitda_proxy_usd'])}</td><td>{mu(fc_row['fcf_usd'])}</td>")
        elif fm and y == fm_y:
            cells = (f"<td>{mu(fm['rev'])} (FY{fm['fy']} model)</td><td>{mu(fm['low'])} to {mu(fm['high'])}</td><td>{model_report.pct(fm['rr'][-1] / fm['rr'][0] - 1, 0)}</td><td>{mu(fm['rr'][-1])}</td>"
                     f"<td>{round(fm['hc']):,} (scenario)</td><td>{mu(fm['ebitda'])}</td><td>{mu(fm['fcf'])}</td>")
        else:
            cells = (f"<td>{P('R10' if fc else 'R1')}</td><td>{P('R10' if fc else 'R2')}</td><td>{P('R10' if fc else 'R1')}</td><td>{P('R10' if fc else 'R1')}</td>"
                     f"<td>{(format(us_staff(y), ',') + ' U.S. at year end ') if us_staff(y) else ''}{P('R3')}</td><td>{P('R6')}</td><td>{P('R6')}</td>")
        w(f"<tr><td>{y}</td><td class='grade'>{'F' if fc else grade(y)}</td>{cells}"
          f"<td class='n'>{money(rr) if rr else ('—' if not fc else P('R10'))}</td><td>{'' if fc else val}</td></tr>")
    w("</table>")
    w(f'<p class="note">Equity + debt raised = Form D amount sold by year of first sale; Form D shows {"debt" if any_debt else "no debt offering"} '
      f'({P("R7", "debt")}). Valuation = latest fund fair value per share in the year where a fund reports shares and fair value; the company '
      f'value needs the share count.</p>')

    # ---------------- year cards
    w('<div class="page"></div><h2>The record, year by year</h2>')
    w('<p class="note">Each card lists what the Form D database and the filings say for that year, then the model line with what is missing.</p>')
    for y in years:
        items = []
        for o in offs:
            if sale_year(o) != y:
                continue
            late = (as_date(o["filing_date"]) - (as_date(o["sale_date"]) or as_date(o["filing_date"]))).days
            kind = ", ".join(k for k, f in (("equity", o["is_equity"]), ("debt", o["is_debt"]), ("options/warrants", o["is_option_to_acquire"]),
                                             ("business combination", o["is_business_combination"])) if f) or "type not stated"
            items.append(f"{fmt_date(o['sale_date'])}: first sale of {'an' if kind[0] in 'aeiou' else 'a'} {kind} offering; {money(o['total_amount_sold'])} sold of "
                         f"{money(o['total_offering_amount'])} offered to {int(o['total_investors'] or 0)} investors "
                         f"(Form {esc(o['submission_type'])} filed {fmt_date(o['filing_date'])}"
                         f"{f', {late // 365} year(s) after the sale' if late > 365 else ''})"
                         + (f". Note: {esc(o['sales_amt_clarification'])}" if o["sales_amt_clarification"] else ""))
        for p in fd["people"]:
            if as_date(p["first"]).year == y:
                items.append(f"Form D lists {esc(p['name'])} as {esc(' and '.join(sorted(p['roles'])).lower())} for the first time "
                             f"(filed {fmt_date(p['first'])})")
        sp = [x for x in fd["spvs"] if as_date(x["first_filing"]).year == y]
        if sp:
            items.append(f"{len(sp)} SPV(s) set up to buy {esc(short)} shares filed Form D: {money(sum(float(x['total_raised'] or 0) for x in sp))} "
                         f"from {sum(int(x['total_investors'] or 0) for x in sp)} investors ({esc('; '.join(x['company_name'] for x in sp[:4]))}"
                         f"{' ...' if len(sp) > 4 else ''})")
        bought, more = set(), 0
        for h in holdings:
            ad = as_date(h.get("acquisition_date"))
            k = (norm_name(h["holder"]), norm_name(h["vehicle"]), ad, h.get("cost_usd"))
            if k in bought:
                continue
            bought.add(k)
            if ad and ad.year == y and h.get("cost_usd"):
                if len(items) >= CARD_LINES:
                    more += 1
                    continue
                items.append(f"{fmt_date(ad)}: {esc(h['holder'])} acquired {esc(short)} {esc(h['security'] or 'shares')} "
                             f"({'via ' + esc(h['vehicle']) if h['vehicle'] and h['vehicle'].lower() != 'direct' else 'direct'}) for "
                             f"{money(h['cost_usd'])} ({esc(h['form'])}, {esc(h['filer'])})")
        for q in nq:
            if q["end"].year == y:
                items.append(f"{q['q']} (N-PORT): {q['funds']} funds, {money(q['value'])}, median <b>{pps(q['median_adj'])} per share</b>"
                             f"{' (filed ' + pps(q['median_filed']) + ')' if q['median_filed'] != q['median_adj'] else ''}")
        for p in pts:
            if p["date"].year == y and p["kind"] == "fair value":
                h = p["src"]
                if len(items) >= CARD_LINES:
                    more += 1
                    continue
                was = f", filed {pps(p['filed'])}" if abs(p["filed"] - p["value"]) > abs(p["value"]) * 0.005 else ""
                items.append(f"{fmt_date(p['date'])}: {esc(p['holder'])} marks its {num(h['shares'])} shares at {money(h['fair_value_usd'])} "
                             f"= <b>{pps(p['value'])} per share</b>{was} ({esc(h['form'])} filed {fmt_date(h['filing_date'])})")
        if more:
            items.append(f"{more} more purchases and fund marks of the year are in the holdings table.")
        for f in facts_by_year.get(y, []):
            if f["metric"] in ("person_role", "investor_link", "product_business") and f["metric"] != "trading_volume":
                continue
            items.append(f"{esc(f['value_text'])} <span class='muted'>({esc(f['form'])}, {esc(f['filer'])})</span>")
        for e in edges:
            if as_date(e["first"]).year == y:
                items.append(f"First filing that states <b>{esc(e['from'])} {esc(e['relation'].replace('_', ' '))} {esc(e['to'])}</b>: "
                             f"“{esc(e['quote'][:160])}” ({esc(e['form'])} filed {fmt_date(e['first'])})")
        n_fil = sum(1 for m in others if as_date(m["file_date"]).year == y)
        if n_fil:
            items.append(f"{n_fil} filing(s) by other companies or funds name {esc(short)} this year")
        if not items:
            items.append(f"No record of {esc(short)} in the sources for this year {P('R11')}")
        model = (f"Model: FY{fm['fy']} revenue {mu(fm['rev'])}; run-rate at year end {mu(fm['rr'][-1])}; about {round(fm['hc']):,} average staff (scenario); gross margin {model_report.pct(fm['gm'])} (peer median); "
                 f"cost per employee {mu(fm['cph'])}; EBITDA proxy {mu(fm['ebitda'])}; FCF proxy {mu(fm['fcf'])}; cumulative capital raised {money(sum(v for k, v in raised.items() if k <= y))}." if fm and y == fm_y else
                 f"Model: revenue {P('R1')}; run-rate at year end {P('R1')}; {(format(us_staff(y), ',') + ' U.S. plan participants at year end; ') if us_staff(y) else ''}average employees {P('R3')}; gross margin {P('R5')}; "
                 f"cost per employee {P('R5')}; EBITDA proxy {P('R6')}; FCF proxy {P('R6')}; cash at year end {P('R6')}; "
                 f"cumulative capital raised {money(sum(v for k, v in raised.items() if k <= y))}.")
        w(f"<div class='card'><h3>{y} · grade {grade(y)}</h3><ul>" + "".join(f"<li>{i}</li>" for i in items) +
          f"</ul><div class='model'>{model}</div></div>")

    # ---------------- revenue
    w('<div class="page"></div><h2>Revenue</h2>')
    w(f'<div class="box">' + (f'{len(rev_facts)} revenue or run-rate statement(s) about {esc(short)} were found in the {len(filings)} filings and are listed below; '
      if rev_facts else f'No revenue, run-rate or ARR figure for {esc(short)} is in the Form D database or in the {len(filings)} filings; ') +
      f'a yearly revenue history needs company revenue statements {P("R1")}' + (f' or volume history {P("R9")}' if volume else '') + '.</div>')
    if statements:
        w('<h3>Volume and revenue statements found in the filings</h3><table><tr><th>Date</th><th>Statement</th><th>Quote</th><th>Source</th></tr>')
        for f in statements:
            w(f"<tr><td>{esc(f.get('as_of_date') or 'not dated')}</td><td>{esc(f['value_text'])}</td><td>“{esc(f['evidence'])}”</td>"
              f"<td>{esc(f['form'])} · {esc(f['filer'])} · {fmt_date(f['filing_date'])} ({f['sources']} filing(s))</td></tr>")
        w("</table>")
    w(f'<table><tr><th>Calendar year</th>' + "".join(f"<th>{y}</th>" for y in years) + "</tr><tr><td>Revenue</td>" +
      "".join(f"<td>{mu(fm['rev']) + ' (FY' + str(fm['fy']) + ' model)' if fm and y == fm_y else P('R1')}</td>" for y in years) + "</tr></table>")

    # ---------------- headcount
    w('<h2>Headcount</h2>')
    w(f'<div class="box">' + (f'{len(emp_facts)} employee statement(s) are listed under "Other statements about the company". ' if emp_facts
      else 'Form D and the filings give no employee count. ') +
      ('Department of Labor Form 5500 gives active plan participants at year end. They are U.S. employees in the plan, not total headcount, and the two plans '
       f'differ a little. A global figure needs H-1B approvals, the U.S. share of staff and the company\'s own headcount statements {P("R3")}.</div>' if plans else
       f'A yearly series needs Department of Labor Form 5500 plan participants (U.S. staff), H-1B approvals and the company\'s own headcount statements {P("R3")}.</div>'))
    cell = lambda v: format(v, ",") if v else P("R3")                                              # noqa: E731
    fy_h1b = {r["fy"]: r["h1b"] for r in fm["yby"]} if fm else {}

    def h1b_cell(y):
        return "—" if fm and y + 1 not in fy_h1b else str(fy_h1b[y + 1]) if fm and fy_h1b[y + 1] is not None else "—" if fm else P("R3")

    w('<table><tr><th>Year-end</th><th>401(k) actives</th><th>Health-plan actives</th><th>U.S. staff used</th><th>Global estimate</th><th>H-1B initial / continuing</th></tr>' +
      "".join(f"<tr><td>{y}</td><td>{cell(plans.get(y, {}).get('k401'))}</td><td>{cell(plans.get(y, {}).get('health'))}</td><td>{cell(us_staff(y))}</td><td>{format(round(fm['health'][y] / fm['cfg']['us_share']), ',') + ' (scenario)' if fm and y in fm['health'] else P('R3')}</td><td>{h1b_cell(y)}</td></tr>" for y in years) + "</table>")
    if plans:
        w('<p class="note">U.S. staff used is the health plan where it was filed, otherwise the 401(k) plan.</p>')
        w('<h3>401(k) plan (Form 5500)</h3><table><tr><th>Plan year</th><th>Form</th><th class="n">Employee deferrals</th><th class="n">Employer contributions</th><th class="n">Net assets, year end</th></tr>' +
          "".join(f"<tr><td>{y}</td><td>{esc(plans[y]['form'])}</td><td class='n'>{money(plans[y]['deferrals'])}</td><td class='n'>{money(plans[y]['employer'])}</td><td class='n'>{money(plans[y]['net_assets'])}</td></tr>"
                  for y in sorted(plans) if "k401" in plans[y]) + "</table>")

    # ---------------- costs, EBITDA, cash
    w('<h2>Costs, EBITDA and cash</h2>')
    w(f'<p class="note">Cost per employee {P("R5")} x employees {P("R3")} against revenue x gross margin {P("R1")} gives the EBITDA proxy '
      f'{P("R6")}. Only the financing side of cash is in the records:</p>')
    w('<table><tr><th>Year</th><th class="n">Capital raised (Form D)</th><th class="n">Cumulative raised</th><th>FCF proxy</th><th>Cash, upper bound</th></tr>')
    cum = 0.0
    for y in years:
        cum += raised.get(y, 0)
        w(f"<tr><td>{y}</td><td class='n'>{money(raised.get(y, 0)) if raised.get(y) else '—'}</td><td class='n'>{money(cum)}</td>"
          f"<td>{P('R6')}</td><td>{money(cum)} + cumulative FCF {P('R6')}</td></tr>")
    w("</table>")

    # ---------------- funding, valuation and customers
    w('<div class="page"></div><h2>Funding, valuation and customers</h2><h3>Form D offerings</h3>')
    w('<table><tr><th>First sale</th><th>Filed</th><th>Accession</th><th>Type</th><th class="n">Offered</th><th class="n">Sold</th>'
      '<th class="n">Investors</th><th>Year</th></tr>')
    for o in offs:
        acc = o["accession_number"]
        url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc.replace('-', '')}/"
        typ = "business combination" if o["is_business_combination"] else ("debt" if o["is_debt"] and not o["is_equity"] else "financing")
        w(f"<tr><td>{esc(as_date(o['sale_date']) or '')}</td><td>{esc(as_date(o['filing_date']))}</td><td><a href='{url}'>{esc(acc)}</a></td>"
          f"<td>{typ}</td><td class='n'>{money(o['total_offering_amount'])}</td><td class='n'>{money(o['total_amount_sold'])}</td>"
          f"<td class='n'>{int(o['total_investors'] or 0)}</td><td>{sale_year(o)}</td></tr>")
    w(f"<tr><td colspan='5'><b>Total (latest figure of each offering)</b></td><td class='n'><b>{money(total_raised)}</b></td>"
      f"<td class='n'>{int(co['total_investors'] or 0)}</td><td></td></tr></table>")
    gap = [y for y in years if y > min(sale_year(o) for o in offs) and not raised.get(y)]
    if gap:
        w(f"<p class='note'>No Form D with a first sale in {', '.join(map(str, gap))} {P('R11')}.</p>")
    w('<h3>Officers and directors named on Form D</h3><table><tr><th>Person</th><th>Role</th><th>First named</th><th>Last named</th><th class="n">Filings</th></tr>')
    for p in fd["people"]:
        w(f"<tr><td>{esc(p['name'])}</td><td>{esc(', '.join(sorted(p['roles'])))}</td><td>{fmt_date(p['first'])}</td>"
          f"<td>{fmt_date(p['last'])}</td><td class='n'>{p['n']}</td></tr>")
    w("</table>")
    if fd["spvs"]:
        w(f'<h3>SPVs set up to buy {esc(short)} shares (Form D, category fund)</h3><table><tr><th>SPV</th><th>First filing</th><th class="n">Raised</th><th class="n">Investors</th></tr>')
        for x in fd["spvs"]:
            w(f"<tr><td>{esc(x['company_name'])}</td><td>{fmt_date(x['first_filing'])}</td><td class='n'>{money(x['total_raised'])}</td>"
              f"<td class='n'>{int(x['total_investors'] or 0)}</td></tr>")
        w(f"<tr><td><b>Total</b></td><td></td><td class='n'><b>{money(spv_total)}</b></td><td class='n'>{spv_inv}</td></tr></table>")
        w("<p class='note'>SPV money buys existing or new shares through the SPV; it may overlap with the company's own offerings.</p>")
    w('<h3>Fund holdings and valuation marks</h3>')
    if allh:
        w(f'<p class="note">Holdings from fund and lender filings: {len(holdings)} row(s) used, {len(allh) - len(holdings)} not verified.{ev_note}</p>')
    if nq:
        nport_section(w, a.nport, nq, split, short)
    loans = [h for h in holdings if h["position_type"] in ("funded_loan", "unfunded_commitment")]
    held = [h for h in holdings if h not in loans]
    if held:
        tally = a.nport_tally if nq else {}
        hits = tally.get("match", 0)
        pool = tally.get("same_fund_rows", 0)
        fixed = sum(v for k, v in getattr(a, "repairs", {}).items() if k != "per_share_hidden")
        hidden = getattr(a, "repairs", {}).get("per_share_hidden", 0)
        checked = (f" {hits:,} of {pool:,} dated values with a Form N-PORT row for the same fund match it, and {tally.get('other_fund', 0)} more match a row of a fund in the same family; "
                   "rows that do not are marked." + (f" {fixed} rows were corrected, using the filing text and the N-PORT marks, so this is not an independent test. {hidden} per-share values "
                                                       "more than 3x from the N-PORT mark are shown as hidden (see the repair column of holdings.csv)." if a.repair else " Rows are shown as extracted, so some amounts carry extraction errors." if ev is None else
                                                       " Amounts are the table rows read by rule; rule repairs do not change them.") if tally else "")
        w(f'<h3>Equity positions in fund filings</h3><p class="note">Per-share values as filed{"; before the " + split_txt + " they are on the old share count" if split else ""}.{checked}</p>')
        w('<table><tr><th>Holder</th><th>Vehicle</th><th>Security</th><th class="n">Shares</th><th class="n">Cost</th><th class="n">Fair value</th>'
          '<th>As of</th><th class="n">Per share</th><th>Source</th></tr>')
        for h in held:
            w(f"<tr><td>{esc(h['holder'])}</td><td>{esc(h['vehicle'])}</td><td>{esc(h['security'])}</td><td class='n'>{num(h.get('shares'))}</td>"
              f"<td class='n'>{money(h.get('cost_usd'))}</td><td class='n'>{money(h.get('fair_value_usd'))}</td>"
              f"<td>{esc(h.get('as_of_date') or 'not stated')}</td><td class='n'>{pps(h['price_per_share']) if h.get('price_per_share') else ('hidden' if 'not used' in h.get('repair', '') else '—')}</td>"
              f"<td><a href='{esc(h['sec_url'])}'>{esc(h['form'])}</a> {fmt_date(h['filing_date'])}{repeat(h)}{' · tier ' + esc(h['evidence_tier']) if h.get('evidence_tier') else ''}{' <b>differs from N-PORT</b>' if h.get('nport_check', '').startswith('value differs') else ''}</td></tr>")
        w("</table>")
        if loans:
            w('<h3>Loans and unfunded commitments</h3><p class="note">Amounts as read from lender filings, not checked against any outside source. Many lenders print in '
              'thousands and some rows here were not scaled, so a loan shown in thousands of dollars may really be in millions. Treat these amounts as unverified.' + (' The credit model does not use these rows; it uses the lender table in its own section.' if fm else '') + '</p><table><tr><th>Lender</th><th>Security</th><th>Type</th><th class="n">Cost</th>'
              '<th class="n">Fair value</th><th class="n">Commitment</th><th>As of</th><th>Source</th></tr>')
            for h in loans:
                w(f"<tr><td>{esc(h['holder'])}</td><td>{esc(h['security'])}</td><td>{esc(h['position_type'].replace('_', ' '))}</td>"
                  f"<td class='n'>{money(h.get('cost_usd'))}</td><td class='n'>{money(h.get('fair_value_usd'))}</td><td class='n'>{money(h.get('commitment_usd'))}</td>"
                  f"<td>{esc(h.get('as_of_date') or 'not stated')}</td><td><a href='{esc(h['sec_url'])}'>{esc(h['form'])}</a> {fmt_date(h['filing_date'])}{repeat(h)}</td></tr>")
            w("</table>")
        chart = svg_marks(pts, "Price per share from fund holdings")
        if chart:
            w(chart + '<div class="cap">Price per share from fund holdings: open dot = cost at purchase, filled dot = fair value at the date of '
                      'the holdings table. Source: fund filings.</div>')
    w(f'<p class="note">{"" if nq else "More marks per quarter " + P("R12") + "; "}Company valuation per round and revenue multiple {P("R4")}.</p>')
    w('<table><tr><th>Round</th><th>Announced</th><th>Amount</th><th>Post-money</th><th>Run-rate then</th><th>Multiple</th></tr>')
    for o in offs:
        w(f"<tr><td>Form D {fmt_date(o['sale_date'])}</td><td>{P('R4')}</td><td>{money(o['total_amount_sold'])}</td><td>{P('R4')}</td>"
          f"<td>{P('R1')}</td><td>{P('R4')}</td></tr>")
    w("</table>")
    w(f'<h3>Customers</h3><table><tr><th>Year end</th><th>Customers / users</th><th>Run-rate per customer</th><th>Revenue per employee</th></tr>' +
      "".join(f"<tr><td>{y}</td><td>{P('R8')}</td><td>{P('R1')}</td><td>{P('R3')}</td></tr>" for y in years[-4:]) + "</table>")

    # ---------------- relationships and legal record (not in the Databricks report)
    if a.records_dir and not fm:
        model_report.records_section(w, Path(a.records_dir))
    if fm:
        model_report.financial_section(w, fm, a.model_data, svg_marks, short)
        model_report.credit_section(w, fm, cm, a.model_data, a.model_out, short)
        model_report.market_section(w, fm, cm, a.model_data, short)
    w('<div class="page"></div><h2>Relationships, people and legal record</h2>')
    if edges:
        w('<table><tr><th>From</th><th>Relation</th><th>To</th><th class="n">Rows</th><th>Filings</th><th>First stated</th><th>Quote</th><th>Direction check</th></tr>')
        for e in edges:
            w(f"<tr><td>{esc(e['from'])}</td><td>{esc(e['relation'].replace('_', ' '))}</td><td>{esc(e['to'])}</td><td class='n'>{e['rows']}</td>"
              f"<td>{len(e['filings'])}</td><td>{fmt_date(e['first'])}</td><td>“{esc(e['quote'][:180])}”</td>"
              f"<td>{esc('; '.join(f'{k} x{v}' for k, v in e['checks'].items()))}</td></tr>")
        w("</table>")
    else:
        w("<p class='note'>No relationship rows (run without --no-llm).</p>")
    roles = [f for f in facts if f["metric"] in ("person_role", "investor_link")]
    if roles:
        w('<h3>People and investors named in other filings</h3><table><tr><th>Statement</th><th>Source</th></tr>')
        for f in roles:
            w(f"<tr><td>{esc(f['value_text'])}</td><td>{esc(f['form'])} · {esc(f['filer'])} · {fmt_date(f['filing_date'])}</td></tr>")
        w("</table>")
    legal = [f for f in facts if f["metric"] in ("legal_event", "regulatory_status")]
    if legal:
        w('<h3>Legal and regulatory record</h3><table><tr><th>Date</th><th>Event</th><th>Source</th></tr>')
        for f in legal:
            w(f"<tr><td>{esc(f.get('as_of_date') or 'not dated')}</td><td>{esc(f['value_text'])}</td>"
              f"<td>{esc(f['form'])} · {esc(f['filer'])} · {fmt_date(f['filing_date'])}</td></tr>")
        w("</table>")
    other = [f for f in facts if f["metric"] in ("product_business", "funding_round", "fund_transaction", "valuation", "debt", "employees", "customers_users", "revenue", "revenue_run_rate", "other")]
    if other:
        w('<h3>Other statements about the company</h3><table><tr><th>Date</th><th>Kind</th><th>Statement</th><th>Source</th></tr>')
        for f in other:
            w(f"<tr><td>{esc(f.get('as_of_date') or 'not dated')}</td><td>{esc(f['metric'].replace('_', ' '))}</td><td>{esc(f['value_text'])}</td>"
              f"<td>{esc(f['form'])} · {esc(f['filer'])}</td></tr>")
        w("</table>")

    # ---------------- forecast
    w(f'<h2>Forecast {fyears[0]}-{fyears[-1]}: the outside view</h2>')
    proj = {}
    if fm:
        for r in model_report.rows(a.model_out, "credit_projections.csv"):
            if r["scenario"] in ("Base", "Downside", "Severe"):
                proj.setdefault(int(r["fiscal_year"][2:]), {})[r["scenario"]] = r

    def forecast_row(y):
        p = proj.get(y)
        if not p:
            return f"<tr><td>{y}</td><td>{P('R10')}</td><td>{P('R10')}</td><td>{P('R10')}</td><td>{P('R3')}</td><td>{P('R6')}</td><td>{P('R6')}</td></tr>"
        b, d = p["Base"], p["Downside"]
        return f"<tr><td>FY{y}</td><td>{mu(b['revenue_usd'])}</td><td>{mu(d['revenue_usd'])}</td><td>{mu(p['Severe']['revenue_usd'])}</td><td>{P('R3')}</td><td>{mu(b['ebitda_proxy_usd'])}</td><td>{mu(b['fcf_usd'])}</td></tr>"

    w('<div class="box">Base and downside scenarios from the credit model. Scenarios, not predictions. All three are in the credit model section.</div>' if fm else
      f'<div class="box">The forecast applies a growth-fade regression fitted on 105 public software companies (SEC XBRL frames) '
      f'to the last observed revenue growth. It needs a revenue history {P("R1")} and the reference class {P("R10")}.</div>')
    w(('<table><tr><th>Year</th><th>Revenue, base</th><th>Revenue, downside</th><th>Revenue, severe</th><th>Employees</th><th>EBITDA proxy, base</th><th>FCF proxy, base</th></tr>' if fm else
       '<table><tr><th>Year</th><th>Central</th><th>P10</th><th>P90</th><th>Employees</th><th>EBITDA proxy</th><th>FCF proxy</th></tr>') +
      "".join(forecast_row(y) for y in fyears) +
      "</table>")

    # ---------------- limits
    fx_cost = sum(float(v.get("meta", {}).get("cost_usd") or 0) for v in per_filing.values())
    w('<h2>Limits</h2><ul>'
      f"<li>No audited figure exists for any year; Form D amounts are self-reported.</li>"
      f"<li>Fund fair values are each fund's own estimate at its reporting date; a per-share value is not a company valuation without the share count.</li>"
      f"<li>Relationships and facts were read by GPT-4.1 from {len(filings)} filings (facts step cost ${fx_cost:.2f}); rows whose quote or numbers "
      f"were not found in the filing text, or with confidence below 50, were dropped. Undated table values are kept in the holdings table but not in the per-share series.</li>"
      f"<li>{ev_note.strip()} The language model is not used to produce holdings numbers.</li>"
      f"<li>The relationship step includes a direction check: the model names the company that does the action and the code flips any edge that "
      f"contradicts it.</li>"
      + (f"<li>The financial and credit models rest on assumptions in model_config: the U.S. share of staff, the peer gross margin and the rating thresholds. A change in these moves the results, as the sensitivity tables show. "
         f"The input tables are built by script from the raw records. Unfunded commitments cover only what the filings tag.</li>" if fm else "") +
      f"<li>The year tables use calendar years{'; the models use fiscal years' if fm else f'; {esc(short)}&#39;s fiscal year end is not in these sources'}.</li></ul>")

    # ---------------- requirements table
    w('<div class="page"></div><h2>Data required to fill the placeholders</h2>')
    w('<p class="note">Every <span class="ph">R..</span> mark in this report is listed here with the form or record that would fill it.</p>')
    w('<table><tr><th>ID</th><th>Missing value</th><th>Required form / record</th><th>Where to get it</th><th>Unlocks</th><th class="n">Places used</th></tr>')
    counts = collections.Counter(re.findall(r'<span class="ph"[^>]*>(?:[^<]* )?(R\d+)</span>', "".join(H)))
    for rid in sorted(P.used, key=lambda r: int(r[1:])):
        v, form, where, unlock = REQUIREMENTS[rid]
        w(f"<tr><td><span class='ph'>{rid}</span></td><td>{esc(v)}</td><td>{esc(form)}</td><td>{esc(where)}</td><td>{esc(unlock)}</td>"
          f"<td class='n'>{counts.get(rid, 0)}</td></tr>")
    w("</table>")

    # ---------------- sources
    w('<h2>Sources</h2><ol class="src">')
    if plans:
        w(f"<li>U.S. Department of Labor, Form 5500 and 5500-SF filings, EIN {esc(next(v['ein'] for v in plans.values() if v.get('ein')))}, plan years {min(plans)} to {plan_last}. https://www.askebsa.dol.gov/</li>")
    w(f"<li>SEC Form D database ({esc(a.formd_db)}), data to {fmt_date(fd['db_until'])}: company CIK {esc(co['cik'])}, "
      f"{len(fd['filings'])} filings; {len(fd['spvs'])} SPVs. https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={esc(co['cik'])}&type=D</li>")
    for m in filings:
        meta = per_filing.get(m["stem"], {}).get("meta", {})
        w(f"<li>{esc(m['form'])} · {esc(m['filer'])} · filed {esc(m['file_date'])} · {esc(m['mentions'])} mention(s), "
          f"{meta.get('unique_paragraphs', '?')} paragraph(s) read · <a href='{esc(m['sec_url'])}'>{esc(m['sec_url'])}</a></li>")
    w("</ol>")
    body = "".join(H)
    doc = (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
           f"<title>{esc(short)} financials</title><style>{CSS}</style></head><body>{body}</body></html>")
    return doc, P.used


# ---------------------------------------------------------------------------------------------- main
def write_csv(path: Path, rows: list[dict], cols: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        wr = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        wr.writeheader()
        wr.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("company", help="folder name under Targeted_pdf/dumps and prefix in coref_verifier/data, e.g. kalshi")
    ap.add_argument("--cik", required=True, help="the company's own CIK in the Form D database, e.g. 0001806928")
    ap.add_argument("--name", default="", help="display / matching name (default: the Form D company name)")
    ap.add_argument("--spv-key", default="", help="word that names the company in SPV names (default: model_config company.spv_key, else first word of the name)")
    ap.add_argument("--formd-db", default=str(DEFAULT_DB))
    ap.add_argument("--model", default=c.DEFAULT_MODEL)
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--extra-folder", action="append", default=[], help="another dump folder of the same company, e.g. databricks_ncsr")
    ap.add_argument("--only-stems", default="", help="text file of filing stems: process only these (for a pilot)")
    ap.add_argument("--refresh-facts", action="store_true")
    ap.add_argument("--rerun-relationships", action="store_true")
    ap.add_argument("--no-llm", action="store_true", help="no model calls: Form D + whatever is cached")
    ap.add_argument("--no-pdf", action="store_true")
    ap.add_argument("--nport-csv", default="", help="output of nport.py build")
    ap.add_argument("--employee-dir", default="", help="folder with retirement_401k_by_year.csv and welfare_plans_by_year.csv (Form 5500)")
    ap.add_argument("--records-dir", default="", help="folder of public-record tables shown when present")
    ap.add_argument("--employee-company", default="", help="company name in the Form 5500 tables")
    ap.add_argument("--repair", action="store_true", help="correct extracted rows by rule (off: rows stay as extracted and are only flagged)")
    ap.add_argument("--model-data", default="", help="folder with data/ and sources/ for the financial and credit models")
    ap.add_argument("--model-config", default="", help="assumptions file, e.g. model_config/databricks.json")
    ap.add_argument("--split", default="", help="stock split as DATE:RATIO, e.g. 2022-08-08:3 (per-share values before it are divided by RATIO)")
    ap.add_argument("--evidence-rows", default="", help="table rows read by rule (evidence_build.py); default out/<company>/evidence/evidence_rows.csv. "
                    "Holdings numbers come from these rows; without the file they stay as the model read them")
    ap.add_argument("--evidence-tiers", default="A,B", help="evidence tiers whose numbers are used; rows of other tiers are only flagged")
    a = ap.parse_args()
    cik = a.cik.zfill(10)
    out = Path(a.out_dir) if a.out_dir else HERE / "out" / a.company
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    print("1. Form D database")
    fd0 = load_formd(Path(a.formd_db), cik, "")
    name = a.name or fd0["company"]["company_name"]
    a.name = name
    if a.model_config and not Path(a.model_config).exists():
        raise SystemExit(f"--model-config {a.model_config}: no such file")
    a.cfg = json.loads(Path(a.model_config).read_text(encoding="utf-8")) if a.model_config else {}
    a.short = short_name(name, a.cfg)
    spv_key = a.spv_key or (a.cfg.get("company") or {}).get("spv_key") or norm_name(name).split()[0]
    fd = load_formd(Path(a.formd_db), cik, spv_key)
    print(f"   {name}: {len(fd['filings'])} filings, {len(fd['offerings'])} offerings, {money(fd['company']['total_raised'])} sold, "
          f"{len(fd['people'])} people, {len(fd['spvs'])} SPVs ('{spv_key}')")

    print("2. Filings that mention the company (prepared pipeline output)")
    only = set(Path(a.only_stems).read_text().split()) if a.only_stems else None
    filings = prepared_filings(a.company, True, a.extra_folder, only)
    print(f"   {len(filings)} filings")

    print("3. Relationships (steps/relationships.py, with the direction check)")
    rels = run_relationships(filings, out, a.model, a.rerun_relationships, a.no_llm)
    print(f"   {fix_lender_direction(rels, name)} lends_to rows corrected (borrower in a loan schedule)")
    edges = rel_edges(rels, name)

    print("4. Facts (holdings, dated facts)")
    limiter = c.RateLimiter(18)
    per_filing = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for m, res in zip(filings, pool.map(lambda m: extract_facts(m, name, out, a.model, a.refresh_facts, a.no_llm, limiter), filings)):
            per_filing[m["stem"]] = res

    print("5. Merge")
    ev_path = Path(a.evidence_rows) if a.evidence_rows else HERE / "out" / a.company / "evidence" / "evidence_rows.csv"   # where evidence_build.py writes, whatever --out-dir
    a.tiers = {x.strip()[:1].upper() for x in a.evidence_tiers.split(",") if x.strip()}
    a.evidence = load_evidence(ev_path)
    print(f"   evidence rows: {'MISSING ' + str(ev_path) + ' - holdings numbers stay as the model read them' if a.evidence is None else str(sum(map(len, a.evidence.values()))) + ' positions in ' + str(len(a.evidence)) + ' filings'}")
    holdings, facts = merge(filings, per_filing, a.evidence, a.tiers, name, add_unread=only is None)
    a.unread = merge.unread
    if a.unread[0]:
        print(f"   {a.unread[1]} evidence positions in {a.unread[0]} filings the mention step did not prepare: {'added' if only is None else 'left out (pilot)'}")
    edges = holder_edges(edges, holdings, name)
    print(f"   {len(holdings)} distinct holding rows ({collections.Counter(h['source'].split(';')[0].split(',')[0] for h in holdings)}), {len(facts)} distinct facts, {len(edges)} relationships involving {name}")

    a.nport = load_nport(a.nport_csv, parse_split(a.split)) if a.nport_csv else []
    a.employee = load_employee(a.employee_dir, a.employee_company or a.short) if a.employee_dir else {}
    a.fin = a.cred = None
    if a.model_data:
        a.model_data, a.model_out = Path(a.model_data), out / "model"
        cfg = a.cfg
        a.fin = model_financials.Model(a.model_data, cfg).run()
        model_financials.save(a.fin, a.model_out)
        a.cred = model_credit.run(a.model_data, a.model_out, cfg, a.model_out)
        print(f"   model: revenue {a.fin['rev'] / 1e9:.3f}B, EBITDA {a.fin['ebitda'] / 1e9:.3f}B, rating {a.cred['rating']}")
    a.repairs = repair_holdings(holdings, a.nport, parse_split(a.split)) if a.repair else collections.Counter()
    print(f"   repairs: {dict(a.repairs) if a.repair else 'off'}")
    a.nport_tally = nport_check(holdings, a.nport) if a.nport else {}
    print(f"   N-PORT check of extracted values: {dict(a.nport_tally)}")
    print("6. Report")
    doc, used = build_html(a, fd, filings, holdings, facts, edges, per_filing)
    stem = f"{a.company}_financials_year_by_year"
    html_path = out / f"{stem}.html"
    html_path.write_text(doc, encoding="utf-8")
    write_csv(out / "formd_offerings.csv", fd["offerings"], list(fd["offerings"][0].keys()) if fd["offerings"] else [])
    write_csv(out / "holdings.csv", holdings, ["holder", "vehicle", "position_type", "security", "verified", "source", "shares", "cost_usd", "fair_value_usd",
                                                "commitment_usd", "shared_with", "value_scale", "shares_scale", "price_per_share",
                                                "cost_per_share", "pct_net_assets", "acquisition_date", "as_of_date", "form", "filer",
                                                "filing_date", "sources", "accessions", "first_filing_date", "page", "evidence_tier", "evidence_row",
                                                "ai_values", "ai_as_of_date", "evidence", "confidence", "sec_url", "nport_check", "repair"])
    if a.nport:
        write_csv(out / "nport_holdings.csv", a.nport, [k for k in a.nport[0] if k not in ("date", "value", "filed", "q", "fund")])
    write_csv(out / "facts.csv", facts, ["as_of_date", "metric", "value_number", "unit", "value_text", "form", "filer", "filing_date",
                                          "sources", "page", "evidence", "confidence", "sec_url"])
    write_csv(out / "relationships_all.csv", rels, ["form", "filing_date", "filer", "page", "from_company", "relation", "to_company",
                                                    "role_sentence", "direction_check", "confidence", "evidence_quote", "sec_url"])
    write_csv(out / "requirements.csv", [{"id": r, "missing_value": REQUIREMENTS[r][0], "required_form": REQUIREMENTS[r][1],
                                          "where": REQUIREMENTS[r][2], "unlocks": REQUIREMENTS[r][3]} for r in used],
              ["id", "missing_value", "required_form", "where", "unlocks"])
    pdf_path = out / f"{stem}.pdf"
    if not a.no_pdf and Path(CHROME).exists():
        r = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", f"--print-to-pdf={pdf_path}",
                            html_path.resolve().as_uri()], capture_output=True, text=True, timeout=180)
        print(f"   PDF: {'ok' if pdf_path.exists() else 'failed ' + r.stderr[-200:]}")
    fx = sum(float(v.get("meta", {}).get("cost_usd") or 0) for v in per_filing.values())
    print(f"done in {time.time() - t0:.0f}s; facts step model cost ${fx:.3f} (includes earlier cached runs)\n   {html_path}\n   {pdf_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
