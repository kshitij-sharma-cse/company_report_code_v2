#!/usr/bin/env python3
"""Stand-in Form D database, for when the team's formd.duckdb is not available. Same four tables build_report.py and
build_inputs.py query (companies, filings, related_persons, offerings), filled from Form D XML notices only.

    export SEC_USER_AGENT="your-org contact@your-domain"
    python tools/build_formd_db.py databricks                       # CIK from model_config/databricks.json
    python tools/build_formd_db.py databricks --xml-dir dumps/databricks/D --out /tmp/formd.duckdb [--no-fetch]

* The company's own D and D/A notices are listed from its EDGAR filing index (data.sec.gov submissions); any not already in
  --xml-dir are downloaded there (raw primary_doc.xml). Filing date and file number come from that index.
* Other issuers' notices found in --xml-dir (an SPV whose Form D names the company) are added as their own companies, with
  category 'fund' when the notice says "Pooled Investment Fund", so build_report.py lists them as SPVs.
* An amendment restates its offering: offerings and company totals use the latest notice of each file number.
Never overwrites an existing database unless --force.
"""
import argparse
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import sec_http as sec

ACC = re.compile(r"(\d{10}-\d{2}-\d{6})")
_subs: dict = {}


def index(cik: str) -> dict:
    """accession -> (form, filing date, file number, primary document) for every filing of the CIK."""
    cik = str(int(cik))
    if cik not in _subs:
        out = {}
        r = sec.get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json")
        if r is not None and r.status_code == 200:
            d = r.json()
            pages = [d["filings"]["recent"]]
            for f in d["filings"].get("files", []):              # older filings are in extra pages
                x = sec.get(f"https://data.sec.gov/submissions/{f['name']}")
                if x is not None and x.status_code == 200:
                    pages.append(x.json())
            for p in pages:
                for i, acc in enumerate(p["accessionNumber"]):
                    out[acc] = (p["form"][i], p["filingDate"][i], p["fileNumber"][i], p["primaryDocument"][i])
        _subs[cik] = out
    return _subs[cik]


def index_page(cik: str, acc: str) -> tuple:
    """Filing date and file number from the filing's own index page (an accession missing from the submissions index)."""
    r = sec.get(f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{acc}-index.htm")
    t = r.text if r is not None and r.status_code == 200 else ""
    fd, fn = re.search(r'Filing Date</div>\s*<div class="info">([^<]+)', t), re.search(r"\b(021-\d+)\b", t)
    return (fd[1].strip() if fd else None), (fn[1] if fn else None)


def parse(p: Path) -> dict | None:
    try:
        root = ET.fromstring(p.read_bytes())
    except ET.ParseError:
        return None
    for e in root.iter():                                         # drop namespaces
        e.tag = e.tag.split("}")[-1]
    od, iss = root.find("offeringData"), root.find("primaryIssuer")
    if od is None or iss is None:
        return None
    one = lambda node, path: (lambda x: x.text.strip() if x is not None and x.text and x.text.strip() else None)(node.find(path) if node is not None else None)   # noqa: E731
    num = lambda x: float(x) if x and re.fullmatch(r"-?\d+(\.\d+)?", x) else None   # noqa: E731  'Indefinite' -> None
    yes = lambda node, path: (one(node, path) or "").lower() == "true"   # noqa: E731
    acc = ACC.search(p.name)
    rels = []
    for rp in root.iter("relatedPersonInfo"):
        rs = [x.text.strip() for x in rp.iter("relationship") if x.text] + [None, None, None]
        rels.append(dict(first_name=one(rp, "relatedPersonName/firstName"), last_name=one(rp, "relatedPersonName/lastName"),
                         relationship_1=rs[0], relationship_2=rs[1], relationship_3=rs[2]))
    sale = od.find("typeOfFiling/dateOfFirstSale")
    return dict(
        cik=f"{int(one(iss, 'cik')):010d}", accession_number=acc[1] if acc else None, filing_date=None, submission_type=one(root, "submissionType"),
        sale_date=one(sale, "value"), file_num=None, is_amendment=yes(od, "typeOfFiling/newOrAmendment/isAmendment"),
        previous_accession_number=one(od, "typeOfFiling/newOrAmendment/previousAccessionNumber"),
        is_equity=yes(od, "typesOfSecuritiesOffered/isEquityType"), is_debt=yes(od, "typesOfSecuritiesOffered/isDebtType"),
        is_option_to_acquire=yes(od, "typesOfSecuritiesOffered/isOptionToAcquireType"),
        is_business_combination=yes(od, "businessCombinationTransaction/isBusinessCombinationTransaction"),
        other_type_desc=one(od, "typesOfSecuritiesOffered/descriptionOfOtherType"), revenue_range=one(od, "issuerSize/revenueRange"),
        total_offering_amount=num(one(od, "offeringSalesAmounts/totalOfferingAmount")), total_amount_sold=num(one(od, "offeringSalesAmounts/totalAmountSold")),
        total_remaining=num(one(od, "offeringSalesAmounts/totalRemaining")), total_investors=num(one(od, "investors/totalNumberAlreadyInvested")),
        minimum_investment=num(one(od, "minimumInvestmentAccepted")), sales_amt_clarification=one(od, "offeringSalesAmounts/clarificationOfResponse"),
        gross_proceeds_clarification=one(od, "useOfProceeds/clarificationOfResponse"), year_of_inc=one(iss, "yearOfInc/value"),
        issuer_name=one(iss, "entityName"), city=one(iss, "issuerAddress/city"), state=one(iss, "issuerAddress/stateOrCountry"),
        jurisdiction_of_inc=one(iss, "jurisdictionOfInc"), industry=one(od, "industryGroup/industryGroupType"), persons=rels)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("company", help="config name; its company.cik is the issuer")
    ap.add_argument("--config", default="")
    ap.add_argument("--cik", default="", help="override the config's CIK")
    ap.add_argument("--xml-dir", action="append", default=[], help="folder(s) of Form D XML named <accession>_*.xml (default: dumps/<co>/D and D-A)")
    ap.add_argument("--out", default=str(sec.ROOT / "input" / "formd" / "formd.duckdb"))
    ap.add_argument("--no-fetch", action="store_true", help="use only the XML already in --xml-dir")
    ap.add_argument("--force", action="store_true", help="replace an existing database file")
    a = ap.parse_args()
    import duckdb
    import pandas as pd
    out = Path(a.out)
    if out.exists() and not a.force:
        raise SystemExit(f"{out} exists (the real Form D database?). Pass --force to replace it, or --out another path.")
    sec.user_agent()
    cik = a.cik or sec.load_config(a.company, a.config).get("company", {}).get("cik", "")
    if not cik:
        raise SystemExit("no CIK: set company.cik in the config or pass --cik")
    cik = f"{int(cik):010d}"
    dirs = [Path(d) for d in a.xml_dir] or [sec.ROOT / "dumps" / a.company / f for f in ("D", "D-A")]
    files = {}
    for d in dirs:
        for p in sorted(d.glob("*.xml")) if d.exists() else []:
            m = ACC.search(p.name)
            if m:
                files.setdefault(m[1], p)
    own = {acc: v for acc, v in index(cik).items() if v[0] in ("D", "D/A")}
    if not a.no_fetch:
        for acc, v in own.items():
            if acc not in files:
                doc = v[3].split("/")[-1] if v[3].lower().endswith(".xml") else "primary_doc.xml"   # xslFormDX01/primary_doc.xml is the rendered page
                r = sec.get(f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}")
                if r is not None and r.status_code == 200 and r.content.lstrip().startswith(b"<?xml"):
                    p = dirs[0] / f"{acc}_{doc}"
                    sec.write_atomic(p, r.content)
                    files[acc] = p
                else:
                    print(f"  ! {acc}: no XML notice (status {getattr(r, 'status_code', None)}); paper-era or non-XML Form D, skipped")
    filings, persons, skipped = [], [], []
    for acc, p in sorted(files.items()):
        f = parse(p)
        if not f:
            skipped.append(p.name)
            continue
        f["accession_number"] = acc
        ix = index(f["cik"]).get(acc)
        f["filing_date"], f["file_num"] = (ix[1], ix[2] or None) if ix else index_page(f["cik"], acc)
        persons += [dict(accession_number=acc, **x) for x in f.pop("persons")]
        filings.append(f)
    if not filings:
        raise SystemExit(f"no Form D notices for CIK {cik} (looked in {', '.join(map(str, dirs))})")
    if not any(f["cik"] == cik for f in filings):   # build_report.py would stop later with 'CIK not in formd.duckdb'
        raise SystemExit(f"no XML Form D notice of CIK {cik} (paper-era, non-XML or not fetched); other issuers only: "
                         + ", ".join(sorted({f"{f['issuer_name']} ({f['cik']})" for f in filings}))[:500])
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    tmp.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    fd = pd.DataFrame(filings)
    pp = pd.DataFrame(persons, columns=["accession_number", "first_name", "last_name", "relationship_1", "relationship_2", "relationship_3"])
    con.execute("CREATE TABLE filings AS SELECT * EXCLUDE (industry) FROM fd")
    con.execute("CREATE TABLE related_persons AS SELECT * FROM pp")
    # one offering per file number (else the accession it amends, else its own); the latest notice states the totals
    con.execute("""CREATE TABLE offerings AS WITH k AS (SELECT *, coalesce(file_num, previous_accession_number, accession_number) AS okey FROM fd)
        SELECT cik, any_value(file_num) AS file_num, arg_min(accession_number, filing_date) AS first_accession, arg_max(accession_number, filing_date) AS last_accession,
        min(sale_date) AS first_sale_date, min(filing_date) AS first_filing_date, max(filing_date) AS last_filing_date,
        arg_max(total_amount_sold, filing_date) AS total_amount_sold, arg_max(total_investors, filing_date) AS total_investors,
        arg_max(industry, filing_date) AS industry FROM k GROUP BY cik, okey""")
    con.execute("""CREATE TABLE companies AS WITH o AS (SELECT cik, sum(total_amount_sold) AS raised, sum(total_investors) AS investors,
        max(total_amount_sold) AS largest FROM offerings GROUP BY cik)
        SELECT f.cik, arg_max(f.issuer_name, f.filing_date) AS company_name,
        CASE WHEN bool_or(f.industry = 'Pooled Investment Fund') THEN 'fund' ELSE 'operating' END AS category,
        any_value(o.raised) AS total_raised, any_value(o.investors) AS total_investors, min(f.filing_date) AS first_filing,
        max(f.filing_date) AS last_filing, any_value(o.largest) AS largest_offering, arg_max(f.city, f.filing_date) AS city,
        arg_max(f.state, f.filing_date) AS state, arg_max(f.jurisdiction_of_inc, f.filing_date) AS jurisdiction_of_inc
        FROM fd f JOIN o USING (cik) GROUP BY f.cik""")
    con.execute("ALTER TABLE offerings DROP COLUMN industry")
    con.execute("ALTER TABLE offerings DROP COLUMN total_investors")
    for t in ("companies", "filings", "related_persons", "offerings"):
        print(t, con.execute(f"SELECT count(*) FROM {t}").fetchone()[0])
    for r in con.execute("SELECT cik, company_name, category, total_raised, total_investors, first_filing, last_filing FROM companies ORDER BY cik").fetchall():
        print(" ", r)
    con.close()
    tmp.replace(out)
    print("written", out, "; not parsed:", skipped or "none")


if __name__ == "__main__":
    main()
