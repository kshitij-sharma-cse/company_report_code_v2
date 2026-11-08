"""SEC/EDGAR helpers: filing sections, full-text SGML submissions, Form D.

* ``SectionTracker`` follows "PART II", "Item 1A. Risk Factors",
  "Schedule of Investments", "Note 3 - Acquisitions", "Exhibit 21" headings
  (line-start headings only, <= 150 chars) so mentions can report where they are.
* ``parse_sgml_submission`` splits an EDGAR full-text ``.txt`` submission into
  header + <DOCUMENT> blocks.
* ``parse_form_d_xml`` / ``parse_form_d_html`` read Form D ``primary_doc.xml``
  (structured) or its EDGAR-rendered HTML (fallback, heuristic).
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

# ------------------------------------------------------------------ sections
_ITEM_RE = re.compile(r"^item\s+(\d{1,2}[a-c]?)\b\s*[.:\-\u2013\u2014]?\s*(.*)$", re.I)
_PART_RE = re.compile(r"^part\s+(iv|i{1,3})\b\.?\s*[.:\-\u2013\u2014]?\s*(.*)$", re.I)
_SOI_RE = re.compile(r"^(?:consolidated\s+)?schedules?\s+of\s+investments\b", re.I)
_NOTE_RE = re.compile(r"^note\s+(\d{1,2})\s*[.:\-\u2013\u2014]+\s*([A-Z].{1,80})$", re.I)
_EXH_RE = re.compile(r"^exhibit\s+(\d{1,3}(?:\.\d{1,3})?)\b\s*[.:\-\u2013\u2014]?\s*(.*)$", re.I)
_SIG_RE = re.compile(r"^signatures?$", re.I)
_TRAILING_PAGE = re.compile(r"\s+\d{1,3}$")


def _title(t: str, n: int = 50) -> str:
    t = _TRAILING_PAGE.sub("", t.strip(" .:-\u2013\u2014|"))
    if not t or not t[0].isupper():
        return ""
    return t if len(t) <= n else t[: n - 1] + "\u2026"


class SectionTracker:
    """Keeps the current Part / Item / sub-section while text is fed in order."""

    def __init__(self, exhibit: str | None = None):
        self.part = self.item = self.sub = None
        self.exhibit = exhibit

    @property
    def label(self) -> str | None:
        if self.exhibit:
            return " > ".join(x for x in (self.exhibit, self.sub) if x)
        parts = [x for x in (self.part, self.item, self.sub) if x]
        return " > ".join(parts) if parts else None

    def feed(self, line: str) -> bool:
        """Update from one line/heading; True if the section changed."""
        line = " ".join(line.replace("|", " ").split())
        if not line or len(line) > 150:
            return False
        m = _PART_RE.match(line)
        if m:
            self.part, self.item, self.sub = f"Part {m.group(1).upper()}", None, None
            mi = _ITEM_RE.match(m.group(2) or "")   # "PART I - Item 1. Business"
            if mi:
                t = _title(mi.group(2))
                self.item = f"Item {mi.group(1).upper()}" + (f". {t}" if t else "")
            return True
        m = _ITEM_RE.match(line)
        if m:
            t = _title(m.group(2))
            self.item = f"Item {m.group(1).upper()}" + (f". {t}" if t else "")
            self.sub = None
            return True
        if _SOI_RE.match(line):
            self.sub = "Schedule of Investments"
            return True
        m = _NOTE_RE.match(line)
        if m:
            self.sub = f"Note {m.group(1)}. {_title(m.group(2))}".rstrip(". ")
            return True
        m = _EXH_RE.match(line)
        if m and (len(line) < 80):
            t = _title(m.group(2))
            self.exhibit = f"Exhibit {m.group(1)}" + (f". {t}" if t else "")
            self.sub = None
            return True
        if _SIG_RE.match(line):
            if not self.exhibit:
                self.part = self.item = None
            self.sub = "Signatures"
            return True
        return False


def exhibit_label(doc_type: str) -> str | None:
    """'EX-21' -> 'Exhibit 21', 'EX-99.1' -> 'Exhibit 99.1'."""
    m = re.match(r"EX-(\d+(?:\.\d+)?)", doc_type or "", re.I)
    return f"Exhibit {m.group(1)}" if m else None


# ------------------------------------------------------------- SGML (.txt)
_DOC_RE = re.compile(r"<DOCUMENT>(.*?)</DOCUMENT>", re.S | re.I)
_TAG_LINE = {k: re.compile(rf"^<{k}>[ \t]*(.*?)[ \t]*$", re.M | re.I)
             for k in ("TYPE", "SEQUENCE", "FILENAME", "DESCRIPTION")}
_TEXT_RE = re.compile(r"<TEXT>(.*?)(?:</TEXT>|\Z)", re.S | re.I)


def looks_like_sgml_submission(head: str) -> bool:
    h = head[:4000].upper()
    return "<SEC-DOCUMENT>" in h or "<SEC-HEADER>" in h or "<IMS-HEADER>" in h or (
        "<DOCUMENT>" in h and "<TYPE>" in h)


def parse_sgml_header(text: str) -> dict:
    m = re.search(r"<(?:SEC|IMS)-HEADER>(.*?)</(?:SEC|IMS)-HEADER>", text, re.S | re.I)
    hdr = m.group(1) if m else text.split("<DOCUMENT>", 1)[0]

    def first(label, block=hdr):
        mm = re.search(rf"^\s*{label}:\s*(.+?)\s*$", block, re.M)
        return mm.group(1).strip() if mm else None

    # first FILER (or ISSUER / SUBJECT COMPANY) block
    blk = hdr
    fm = re.search(r"^\s*(FILER|ISSUER|FILED BY|SUBJECT COMPANY):\s*$", hdr, re.M)
    if fm:
        nxt = re.search(r"^\s*(FILER|ISSUER|FILED BY|SUBJECT COMPANY|REPORTING-OWNER):\s*$",
                        hdr[fm.end():], re.M)
        blk = hdr[fm.end(): fm.end() + nxt.start()] if nxt else hdr[fm.end():]
    return {
        "name": first("COMPANY CONFORMED NAME", blk),
        "cik": first("CENTRAL INDEX KEY", blk),
        "form_type": first("CONFORMED SUBMISSION TYPE"),
        "accession_number": first("ACCESSION NUMBER"),
        "period_of_report": first("CONFORMED PERIOD OF REPORT"),
        "filed_as_of": first("FILED AS OF DATE"),
        "former_names": re.findall(r"^\s*FORMER CONFORMED NAME:\s*(.+?)\s*$", blk, re.M),
        "source": "sgml-header",
    }


def parse_sgml_submission(text: str) -> tuple[dict, list[dict]]:
    header = parse_sgml_header(text)
    docs = []
    for m in _DOC_RE.finditer(text):
        block = m.group(1)
        d = {k.lower(): (r.search(block).group(1) if r.search(block) else "") for k, r in _TAG_LINE.items()}
        tm = _TEXT_RE.search(block)
        d["body"] = tm.group(1) if tm else ""
        docs.append(d)
    return header, docs


_BINARY_EXT = {".jpg", ".jpeg", ".gif", ".png", ".bmp", ".tif", ".tiff", ".pdf", ".zip",
               ".xls", ".xlsx", ".doc", ".docx", ".ppt", ".pptx", ".xsd", ".json", ".css", ".js"}
_SKIP_TYPES = ("GRAPHIC", "ZIP", "EXCEL", "PDF", "JSON", "XML", "EX-101", "EX-102", "EX-103")


def classify_sgml_document(d: dict) -> str:
    """'html' | 'text' | 'form_d' | 'skip:<reason>'"""
    body = d["body"].lstrip()
    fname = (d.get("filename") or "").lower()
    dtype = (d.get("type") or "").upper()
    stripped = re.sub(r"^(?:<(?:PDF|XBRL|XML)>\s*)+", "", body, flags=re.I)
    if re.match(r"begin \d{3} ", stripped):
        return "skip:uuencoded binary"
    ext = fname[fname.rfind("."):] if "." in fname else ""
    if "<edgarSubmission" in body[:3000]:
        return "form_d"
    if ext in _BINARY_EXT:
        return f"skip:binary/data file ({ext})"
    if dtype.startswith(_SKIP_TYPES) or ext == ".xml" and "<html" not in body[:5000].lower():
        return f"skip:machine-readable ({dtype or ext})"
    if re.search(r"<html|<xbrl>|<body|<p[\s>]|<div[\s>]|<table", body[:20000], re.I):
        return "html"
    return "text"


def strip_sgml_wrappers(body: str) -> str:
    return re.sub(r"</?(?:XBRL|XML)>", "", body, flags=re.I)


# ------------------------------------------------------------------ Form D
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _kids(el, name):
    return [c for c in el if _local(c.tag) == name]


def _kid(el, name):
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _txt(el, *path) -> str | None:
    cur = el
    for p in path:
        if cur is None:
            return None
        cur = _kid(cur, p)
    if cur is None or cur.text is None:
        return None
    return " ".join(cur.text.split()) or None


def _kids_or_empty(el):
    return list(el) if el is not None else []


def _all_text_values(el) -> list[str]:
    if el is None:
        return []
    out = []
    for c in el.iter():
        if c is el:
            continue
        t = " ".join((c.text or "").split())
        if t and t.lower() not in ("none", "n/a", "na"):
            out.append(t)
    return out


def is_form_d_xml(data: bytes | str) -> bool:
    head = data[:3000] if isinstance(data, str) else data[:3000].decode("utf-8", "ignore")
    return "<edgarSubmission" in head and ("<primaryIssuer" in (data if isinstance(data, str)
                                                                 else data.decode("utf-8", "ignore")))


def parse_form_d_xml(data: bytes | str) -> dict | None:
    """Parse Form D primary_doc.xml (namespace-agnostic, tolerant).

    Element names follow EDGAR Form D XML (schemaVersion X0708) as observed in
    filed documents: primaryIssuer/{cik, entityName, issuerAddress,
    jurisdictionOfInc, issuerPreviousNameList/value, edgarPreviousNameList/value,
    entityType, yearOfInc}, relatedPersonsList/relatedPersonInfo, offeringData/...
    Additional co-issuers are taken from any other element that has an
    ``entityName`` child (exact container name not verified)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    root = ET.fromstring(data.strip())
    if _local(root.tag) != "edgarSubmission":
        return None

    issuers = []
    for el in root.iter():
        if _kid(el, "entityName") is not None and _local(el.tag) != "signature":
            issuers.append({
                "role": "primary" if _local(el.tag) == "primaryIssuer" else "additional",
                "cik": _txt(el, "cik"),
                "entity_name": _txt(el, "entityName"),
                "previous_names": _all_text_values(_kid(el, "issuerPreviousNameList")),
                "edgar_previous_names": _all_text_values(_kid(el, "edgarPreviousNameList")),
                "entity_type": _txt(el, "entityType"),
                "entity_type_other": _txt(el, "entityTypeOtherDesc"),
                "jurisdiction": _txt(el, "jurisdictionOfInc"),
                "year_of_inc": _txt(el, "yearOfInc", "value"),
                "city": _txt(el, "issuerAddress", "city"),
                "state": _txt(el, "issuerAddress", "stateOrCountryDescription")
                or _txt(el, "issuerAddress", "stateOrCountry"),
            })
    issuers.sort(key=lambda x: x["role"] != "primary")

    persons = []
    for rp in root.iter():
        if _local(rp.tag) != "relatedPersonInfo":
            continue
        nm = _kid(rp, "relatedPersonName")
        name = " ".join(x for x in (_txt(nm, "firstName"), _txt(nm, "middleName"),
                                    _txt(nm, "lastName")) if x) if nm is not None else ""
        rels = [" ".join((r.text or "").split()) for r in rp.iter() if _local(r.tag) == "relationship"]
        persons.append({"name": name, "relationships": [r for r in rels if r],
                        "clarification": _txt(rp, "relationshipClarification")})

    off = _kid(root, "offeringData")
    offering = {}
    recipients, signatures, clarifications = [], [], []
    if off is not None:
        offering = {
            "industry_group": _txt(off, "industryGroup", "industryGroupType"),
            "investment_fund_type": _txt(off, "industryGroup", "investmentFundInfo", "investmentFundType"),
            "revenue_range": _txt(off, "issuerSize", "revenueRange"),
            "aggregate_nav_range": _txt(off, "issuerSize", "aggregateNetAssetValueRange"),
            "federal_exemptions": [" ".join((i.text or "").split()) for i in
                                   _kids_or_empty(_kid(off, "federalExemptionsExclusions"))],
            "is_amendment": _txt(off, "typeOfFiling", "newOrAmendment", "isAmendment"),
            "date_of_first_sale": _txt(off, "typeOfFiling", "dateOfFirstSale", "value"),
            "first_sale_yet_to_occur": _txt(off, "typeOfFiling", "dateOfFirstSale", "yetToOccur"),
            "total_offering_amount": _txt(off, "offeringSalesAmounts", "totalOfferingAmount"),
            "total_amount_sold": _txt(off, "offeringSalesAmounts", "totalAmountSold"),
            "total_remaining": _txt(off, "offeringSalesAmounts", "totalRemaining"),
            "minimum_investment": _txt(off, "minimumInvestmentAccepted"),
            "has_non_accredited_investors": _txt(off, "investors", "hasNonAccreditedInvestors"),
            "number_already_invested": _txt(off, "investors", "totalNumberAlreadyInvested"),
        }
        for r in off.iter():
            if _local(r.tag) == "recipient":
                recipients.append({k: (None if (v or "").lower() in ("none", "n/a") else v) for k, v in {
                    "name": _txt(r, "recipientName"),
                    "crd": _txt(r, "recipientCRDNumber"),
                    "associated_bd": _txt(r, "associatedBDName"),
                    "associated_bd_crd": _txt(r, "associatedBDCRDNumber")}.items()})
            elif _local(r.tag) == "signature":
                signatures.append({"issuer_name": _txt(r, "issuerName"),
                                   "signer": _txt(r, "nameOfSigner"),
                                   "title": _txt(r, "signatureTitle"),
                                   "date": _txt(r, "signatureDate")})
    for parent in root.iter():
        for c in parent:
            if _local(c.tag) == "clarificationOfResponse" and (c.text or "").strip():
                clarifications.append({"field": _local(parent.tag), "text": " ".join(c.text.split())})

    return {
        "source": "xml",
        "submission_type": _txt(root, "submissionType"),
        "schema_version": _txt(root, "schemaVersion"),
        "issuers": issuers,
        "related_persons": persons,
        "offering": offering,
        "sales_compensation_recipients": recipients,
        "signatures": signatures,
        "clarifications": clarifications,
    }


def parse_form_d_html(soup) -> dict | None:
    """Heuristic parser for the EDGAR-rendered Form D HTML (xslFormDX01).

    Relies on the rendering's table summaries ("Issuer Identity Information",
    "Related Persons") and label cells (<th>Name of Issuer</th> followed by a
    row with <td class="FormData">).  Layout details beyond what was observed in
    rendered filings (e.g. how several previous names are shown) are uncertain."""
    ident = soup.find_all("table", attrs={"summary": re.compile(r"Issuer Identity", re.I)})
    if not ident:
        return None
    issuers = []
    for k, t in enumerate(ident):
        cik = None
        a = t.find("a", href=re.compile(r"CIK=", re.I))
        if a:
            cik = a.get_text(strip=True)
        else:
            m = re.search(r"\b\d{10}\b", t.get_text(" "))
            cik = m.group() if m else None

        def after_label(label):
            th = t.find(lambda x: x.name in ("th", "td") and
                        " ".join(x.get_text(" ").split()).lower() == label.lower())
            if not th:
                return None
            tr = th.find_parent("tr")
            nxt = tr.find_next_sibling("tr") if tr else None
            if nxt:
                cell = nxt.find("td", class_="FormData") or nxt.find("td")
                if cell:
                    return " ".join(cell.get_text(" ").split()) or None
            return None

        prev = []
        rows = t.find_all("tr", recursive=False) or (t.tbody.find_all("tr", recursive=False) if t.tbody else [])
        if len(rows) > 1:
            cells = rows[1].find_all("td", recursive=False)
            if len(cells) > 1:
                prev = [p.strip() for p in cells[1].get_text("\n").split("\n")
                        if p.strip() and p.strip().lower() != "none"]
        etype = None
        for cb in t.find_all("td", class_="CheckBox"):
            if cb.get_text(strip=True) == "X":
                lab = cb.find_next_sibling("td")
                if lab and lab.get_text(strip=True) not in ("None",):
                    etype = lab.get_text(strip=True)
                    break
        issuers.append({"role": "primary" if k == 0 else "additional", "cik": cik,
                        "entity_name": after_label("Name of Issuer"),
                        "previous_names": prev, "edgar_previous_names": [],
                        "entity_type": etype,
                        "jurisdiction": after_label("Jurisdiction of Incorporation/Organization")})
    persons = []
    for t in soup.find_all("table", attrs={"summary": re.compile(r"Related Persons", re.I)}):
        tds = [td for td in t.find_all("td", class_="FormData")][:3]
        if tds:
            last, first, middle = ([" ".join(td.get_text(" ").split()) for td in tds] + ["", "", ""])[:3]
            persons.append({"name": " ".join(x for x in (first, middle, last) if x),
                            "relationships": [], "clarification": None})
    return {"source": "html", "submission_type": "D", "issuers": issuers,
            "related_persons": persons, "offering": {}, "sales_compensation_recipients": [],
            "signatures": [], "clarifications": []}


def form_d_fields(rec: dict) -> list[tuple[str, str, str, str | None]]:
    """Flatten a Form D record into (field, section, text, cik) for matching."""
    out = []
    for k, iss in enumerate(rec.get("issuers", [])):
        if iss.get("entity_name"):
            out.append((f"issuer[{k}].entityName", "Form D: Issuer", iss["entity_name"], iss.get("cik")))
        for n in iss.get("previous_names", []):
            out.append((f"issuer[{k}].issuerPreviousName", "Form D: Previous names", n, None))
        for n in iss.get("edgar_previous_names", []):
            out.append((f"issuer[{k}].edgarPreviousName", "Form D: Previous names", n, None))
    for k, p in enumerate(rec.get("related_persons", [])):
        if p.get("name"):
            out.append((f"relatedPerson[{k}]", "Form D: Related persons", p["name"], None))
        if p.get("clarification"):
            out.append((f"relatedPerson[{k}].clarification", "Form D: Related persons",
                        p["clarification"], None))
    for k, r in enumerate(rec.get("sales_compensation_recipients", [])):
        for f in ("name", "associated_bd"):
            if r.get(f):
                out.append((f"recipient[{k}].{f}", "Form D: Sales compensation", r[f], None))
    for k, s in enumerate(rec.get("signatures", [])):
        if s.get("issuer_name"):
            out.append((f"signature[{k}].issuerName", "Form D: Signature", s["issuer_name"], None))
    for k, c in enumerate(rec.get("clarifications", [])):
        out.append((f"clarification[{k}].{c['field']}", "Form D: Clarification", c["text"], None))
    return out
