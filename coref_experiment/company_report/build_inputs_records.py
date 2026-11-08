"""Public-record tables that need no model: trials, device approvals, payments, awards, loans, grants and agency sentences. Each table is written only if its source exists."""
from __future__ import annotations

import collections
import csv
import html
import json
import re
from pathlib import Path

from build_inputs import write


def _latest(folder: Path, pattern: str):
    f = sorted(folder.glob(pattern), key=lambda p: (len(p.name), p.name))
    return f[-1] if f else None


def trials(raw: Path, out: Path) -> dict:
    p = _latest(raw / "clinicaltrials", "studies_*.json")
    if not p:
        return {}
    body = []
    for s in json.load(open(p, encoding="utf-8")).get("studies", []):
        m = s["protocolSection"]
        i, st, sp, d = m["identificationModule"], m.get("statusModule", {}), m.get("sponsorCollaboratorsModule", {}), m.get("designModule", {})
        body.append([i.get("nctId"), i.get("briefTitle"), sp.get("leadSponsor", {}).get("name"), st.get("overallStatus"), st.get("startDateStruct", {}).get("date"), d.get("enrollmentInfo", {}).get("count")])
    body.sort(key=lambda r: str(r[4]))
    return {"clinical_trials.csv": write(out, "clinical_trials.csv", ["nct", "title", "sponsor", "status", "start", "enrollment"], body)}


def fda(raw: Path, out: Path) -> dict:
    res = {}
    p = _latest(raw / "fda", "openfda_pma_*.json")
    if p:
        r = json.load(open(p, encoding="utf-8")).get("results", [])
        res["fda_pma.csv"] = write(out, "fda_pma.csv", ["pma", "supplement", "decision_date", "decision", "trade_name", "supplement_type", "reason"],
                                   sorted([[x.get("pma_number"), x.get("supplement_number"), x.get("decision_date"), x.get("decision_code"), x.get("trade_name"), x.get("supplement_type"), x.get("supplement_reason")] for x in r], key=lambda t: t[2] or ""))
    p = _latest(raw / "fda", "openfda_udi_*.json")
    if p:
        r = json.load(open(p, encoding="utf-8")).get("results", [])
        res["fda_udi.csv"] = write(out, "fda_udi.csv", ["brand", "model", "company", "published"], [[x.get("brand_name"), x.get("version_or_model_number"), x.get("company_name"), x.get("publish_date")] for x in r])
    return res


def open_payments(raw: Path, out: Path) -> dict:
    d = raw / "cms_open_payments"
    if not d.exists():
        return {}
    by = collections.defaultdict(lambda: [0, 0.0])
    nature = collections.defaultdict(lambda: [0, 0.0])
    state = collections.Counter()
    for p in sorted(d.glob("open_payments_*_*_*.json")):
        kind = "research" if "_research_" in p.name else "general"
        for r in json.load(open(p, encoding="utf-8")).get("results", []):
            if kind == "research" and "_2025_" in p.name and "_2024_" in str(p):
                continue
            v = float(r.get("total_amount_of_payment_usdollars") or 0)
            k = (r.get("program_year"), kind)
            by[k][0] += 1
            by[k][1] += v
            if kind == "general":
                nature[r.get("nature_of_payment_or_transfer_of_value")][0] += 1
                nature[r.get("nature_of_payment_or_transfer_of_value")][1] += v
                state[r.get("recipient_state")] += 1
    return {"open_payments.csv": write(out, "open_payments.csv", ["program_year", "kind", "payments", "amount_usd"], [[k[0], k[1], v[0], round(v[1], 2)] for k, v in sorted(by.items(), key=lambda t: str(t[0]))]),
            "open_payments_nature.csv": write(out, "open_payments_nature.csv", ["nature", "payments", "amount_usd"], [[k, v[0], round(v[1], 2)] for k, v in sorted(nature.items(), key=lambda t: -t[1][1])]),
            "open_payments_state.csv": write(out, "open_payments_state.csv", ["state", "payments"], state.most_common(10))}


def awards(raw: Path, out: Path) -> dict:
    seen, body = set(), []
    for p in sorted((raw / "usaspending").glob("search_*.json")):
        for r in json.load(open(p, encoding="utf-8")).get("results", []):
            k = r.get("Award ID")
            if k in seen:
                continue
            seen.add(k)
            body.append([k, r.get("Recipient Name"), r.get("Awarding Agency"), r.get("Award Type") or r.get("Contract Award Type") or "", r.get("Start Date"), r.get("End Date"), r.get("Award Amount") or r.get("Loan Value"), (r.get("Description") or "")[:110]])
    return {"federal_awards.csv": write(out, "federal_awards.csv", ["award_id", "recipient", "agency", "type", "start", "end", "amount_usd", "description"], sorted(body, key=lambda r: str(r[4])))}


def canada(raw: Path, out: Path) -> dict:
    body = []
    for p in sorted((raw / "canada").glob("*.html")):
        t = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", p.read_text(encoding="utf-8", errors="ignore"))))
        g = lambda k, e: (re.search(k + r"\s*(.*?)\s*" + e, t) or [None, ""])[1]                   # noqa: E731
        body.append([g("Reference Number:", "Agreement Type"), g("Recipient's Legal Name:", "Program Purpose"), g("Organization:", "Location"), g("Agreement Value:", "Agreement Date"), g("Agreement Date:", r"- 0001|Organization")[:12]])
    return {"canada_grants.csv": write(out, "canada_grants.csv", ["reference", "recipient", "organization", "value", "date"], body)}


def ppp(emp_data: Path, cfg: dict, out: Path) -> dict:
    co = cfg["company"]["employee_company"]
    r = [x for x in csv.DictReader(open(emp_data / "ppp_loans.csv", encoding="utf-8", newline="")) if x["company"] == co]
    return {"ppp_loans.csv": write(out, "ppp_loans.csv", ["date_approved", "borrower", "initial_amount", "final_amount", "jobs", "forgiven", "lender"], [[x["date_approved"], x["borrower_name"], x["initial_approval_amount"], x["current_approval_amount"], x["jobs_reported"], x["forgiveness_amount"], x["lender"]] for x in r])}


def statements(raw: Path, cfg: dict, out: Path) -> dict:
    """Sentences naming the company or its product in Federal Register documents."""
    key = "|".join(re.escape(a) for a in cfg["company"].get("aliases", []))
    body = []
    for p in sorted((raw / "federal_register").glob("*.txt")):
        t = re.sub(r"\s+", " ", p.read_text(encoding="utf-8", errors="ignore"))
        for s in re.split(r"(?<=[.;])\s+", t):
            if re.search(key, s, re.I):
                body.append([p.name[:30], s[:320].replace("[supreg]", "")])
    body.sort(key=lambda r: "$" not in r[1])   # sentences with an amount first
    return {"agency_statements.csv": write(out, "agency_statements.csv", ["document", "sentence"], body)}


def formd_notices(raw: Path, out: Path) -> dict:
    """Each offering from its latest raw notice, chained through the previous accession number."""
    latest, prev = {}, {}
    for p in sorted((raw / "sec_form_d").glob("*primary_doc.xml")):
        t = p.read_text(encoding="utf-8", errors="ignore")
        g = lambda k: (re.search(rf"<{k}>\s*(.*?)\s*</{k}>", t, re.S) or [None, ""])[1]           # noqa: E731
        acc = p.name[:20]
        first = re.search(r"<dateOfFirstSale>\s*<value>(.*?)<", t)
        kind = "debt" if "<isDebtType>true" in t else "equity"
        latest[acc] = [acc, g("signatureDate"), first[1] if first else "", kind, g("totalOfferingAmount"), g("totalAmountSold"), g("totalNumberAlreadyInvested")]
        if g("previousAccessionNumber"):
            prev[acc] = g("previousAccessionNumber")
    root = {}
    for acc in sorted(latest):
        r = acc
        while r in prev:
            r = prev[r]
        root[r] = acc
    return {"formd_notices.csv": write(out, "formd_notices.csv", ["latest_notice", "signed", "first_sale", "kind", "offering_amount", "amount_sold", "investors"], [latest[a] for a in sorted(root.values())])}
