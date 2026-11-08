"""Approach D: relationships between companies (supplier / buyer / manufacturer / any kind).

Input   the pipeline's result for one PDF (kept mentions after string match + LLM disambiguation:
        *_mentions.csv or *_final.json) and the PDF itself.
Nodes   the companies in that result, plus the filer (the company that submits the document).
        By default the LLM may also report companies that are NOT on your list (TSMC, a distributor...), flagged
        "not_on_list", so you can see what the master list misses. --listed-only switches that off.
Method  1. each kept mention is located in a PDF paragraph (page + passage text);
        2. candidate paragraphs: (a) paragraphs that mention a listed company other than the filer (the filer is
           implicit through "we / our / the Company"); (b) unless --listed-only, paragraphs that carry a business
           cue word (supplier, customer, distributor, foundry, manufactur-, licens-...) and a company-like name;
        3. the LLM reads candidates in parallel chunks (previous and next paragraph as context) and returns
           relationships "from -> to" with a fixed relation vocabulary, a verbatim evidence quote and confidence;
        4. rows are validated (ids allowed, quote is in the paragraph or its context, quote names the other
           company, unlisted names appear in the text) and merged into edges.
Output  out/D_<pdf>_<model>.json (meta, per-call log, nodes, edges, raw rows) and the edges as CSV.

Usage:
  company-finder/.venv/bin/python coref_experiment/approach_d_relations.py PDF --mentions RESULT.csv|RESULT.json
        [--filer "Ambiq"] [--listed-only] [--chunk-tokens 4000] [--workers 6] [--rpm 18] [--model M] [--dry-run]
"""
from __future__ import annotations

import argparse
import ast
import collections
import csv
csv.field_size_limit(10**8)
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import common as c

# direction: the relation is read as "<from> <relation> <to>"
RELATIONS = {
    "supplies": "from supplies goods, components, materials, equipment or software to (to = the buyer/customer)",
    "manufactures_for": "from manufactures / fabricates / assembles products for (a foundry or contract manufacturer is the from)",
    "provides_services_to": "from provides services (consulting, hosting, testing, logistics, support...) to",
    "licenses_to": "from licenses technology / IP / software to (only when the text says who licenses to whom)",
    "distributes_for": "from distributes / resells products made by (a distributor or reseller is the from)",
    "invests_in": "from holds shares in or invests in",
    "lends_to": "from lends money / provides financing to",
    "acquired": "from acquired / merged with",
    "subsidiary_of": "from is a subsidiary or affiliate of",
    "underwrites_for": "from underwrites / places the securities of",
    "advises": "from advises or acts as agent / counsel / auditor for",
    "partners_with": "joint development, collaboration, alliance, or a two-way arrangement whose direction is not stated (symmetric)",
    "competes_with": "competitors (symmetric)",
    "other": "any other business relationship; describe it in detail",
}
SYMMETRIC = {"partners_with", "competes_with"}

SYSTEM = """You extract business RELATIONSHIPS between companies from paragraphs of a financial/legal document
(prospectus, annual report, Form C, fund filing...).

FILER: {filer} (id {fid}). The filer is the company that submits this document. In its own text "we", "our", "us",
"the Company" (and sometimes "it") refer to the filer.

Input format: a paragraph to analyse is shown as
  [P<n>|p<page>] text
  COMPANIES: <id> <name>; <id> <name>; ...        <- listed companies in that paragraph
Lines starting with [ctx] are the neighbouring paragraphs, given only to resolve pronouns and lists; do not report
relationships that exist only in them.

Report a relationship only when the paragraph states or clearly implies one between TWO companies (the filer counts
as a company, also when it appears only as "we/our/the Company"). Read it as "<from> <relation> <to>":
{relations}

Companies:
- Use ids from that paragraph's COMPANIES line, or the filer id {fid}.
{unlisted_rule}

How to read common sentences:
- "Net sales from <distributor> were 14% of our net sales" / "we sell through distributors such as D"
    -> from=D, distributes_for, to=filer.
- "Our major end customers included G and W" -> from=filer, supplies, to=G; and from=filer, supplies, to=W.
- "We rely on F, a foundry, to manufacture our chips" -> from=F, manufactures_for, to=filer.
- "We buy components / IP / software from V" -> from=V, supplies (or licenses_to), to=filer.
- "arrangements with third parties such as A and B" (no direction stated) -> partners_with, confidence at most 60.
- Competitors listed together, board members' other jobs, auditors' names in signatures, trademark owners and
  stock-exchange or regulator names are NOT business relationships (a named auditor, law firm or underwriter
  acting for the filer is: advises / underwrites_for).

Rules:
- One row per (paragraph, from, to, relation); several companies in one sentence = several rows.
- evidence: a short VERBATIM quote (max 25 words) from the paragraph that supports the relation AND contains the
  name (or obvious short form) of the non-filer company. If no such quote exists, do not report the relationship.
- detail: what is supplied/provided, max 8 words (e.g. "wafer fabrication").
- confidence: integer 0-100. Use at most 60 when the direction or the type is not explicit.
Return JSON only: {{"references": [...]}}; empty list when the chunk has no relationship."""

UNLISTED_ON = ('- A company named in the text but NOT on the COMPANIES line is allowed: use id "NEW" and put its full '
               'name as written in from_name / to_name. Never invent a company: the name must appear in the paragraph. '
               'Do not report people, products, places or generic terms as companies.')
UNLISTED_OFF = '- Only listed ids are allowed. Ignore companies that are not on the COMPANIES line.'

SCHEMA = {
    "name": "relations", "strict": True,
    "schema": {
        "type": "object", "additionalProperties": False, "required": ["references"],
        "properties": {"references": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["para", "from_id", "from_name", "to_id", "to_name", "relation", "detail", "evidence",
                         "confidence"],
            "properties": {
                "para": {"type": "integer"}, "from_id": {"type": "string"}, "from_name": {"type": "string"},
                "to_id": {"type": "string"}, "to_name": {"type": "string"},
                "relation": {"type": "string", "enum": list(RELATIONS)}, "detail": {"type": "string"},
                "evidence": {"type": "string"}, "confidence": {"type": "integer"}}}}},
    },
}

CUE_RX = re.compile(r"\b(supplier|supplies|supplied|supply agreement|customers?|distributors?|resellers?|foundr(?:y|ies)|"
                    r"manufactur\w*|fabricat\w*|assembl\w*|vendors?|licens\w*|purchase[sd]?|contract(?:ed|s)? with|"
                    r"agreement with|partner(?:ed|s|ship)?|collaborat\w*|outsourc\w*|sole source|single source)\b", re.I)
NAME_RX = re.compile(r"\b(?:[A-Z][\w&.'’-]*\s){1,5}(?:Inc\.?|Corp\.?|Corporation|LLC|L\.L\.C\.|L\.P\.|LP|Ltd\.?|Limited|"
                     r"Co\.|Company|plc|N\.V\.|S\.A\.|AG|GmbH|Holdings|Group|Bank|Partners|Capital|Pte\.?)(?=\W)")
ACRONYM_RX = re.compile(r"\b[A-Z]{3,6}\b")
ACRONYM_STOP = {"SEC", "GAAP", "USD", "CEO", "CFO", "COO", "CTO", "IPO", "NYSE", "ESG", "LLC", "ARR", "AND", "THE", "FOR",
                "RISK", "ITEM", "NOTE", "PART", "TABLE", "INC", "LTD", "EBITDA", "FASB", "IRS", "SOC", "SOCS", "MCU",
                "MCUS", "IOT", "API", "AI", "CPU", "GPU", "PDF", "USA", "EU", "UK", "RTOS", "MIPS", "IP", "IC", "ICS",
                "OEM", "ODM", "R&D", "SG&A", "COGS", "ASC", "FOB", "FCPA", "OFAC", "DOJ", "NASDAQ", "FINRA", "PCAOB",
                "BLE", "DSP", "RISC", "DRAM", "SRAM", "NVM", "MRAM", "ADC", "SPOT", "SPI", "USB", "CAGR", "TAM",
                "SAM", "WIFI", "WI", "FI"}


# ---------------------------------------------------------------- load the pipeline result
def _extras(v):
    if isinstance(v, dict):
        return v
    for f in (json.loads, ast.literal_eval):
        try:
            out = f(v)
            return out if isinstance(out, dict) else {}
        except Exception:  # noqa: BLE001
            pass
    return {}


def load_mentions(path: str, pdf_name: str) -> list[dict]:
    p = Path(path)
    if p.suffix.lower() == ".json":
        rows = json.loads(p.read_text(encoding="utf-8")).get("mentions", [])
    else:
        rows = list(csv.DictReader(open(p, encoding="utf-8")))
    out = []
    for r in rows:
        if r.get("document") and Path(str(r["document"])).name != pdf_name:
            continue
        ex = _extras(r.get("extras"))
        if ex.get("llm_disambiguation", {}).get("is_company") is False:
            continue  # defensive: a dropped row in a matching-only file
        out.append({"company_id": str(r.get("company_id")), "name": r.get("canonical_name") or r.get("company_name"),
                    "text": re.sub(r"\s+", " ", r.get("matched_text") or "").strip(), "page": int(float(r.get("page") or r.get("page_number") or 0)),
                    "passage": r.get("passage") or "", "context": r.get("context") or "",
                    "is_filer": str(r.get("is_filer")).lower() == "true"})
    return out


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def has_word(text: str, word: str) -> bool:
    return bool(word) and bool(re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text, re.I))


def _snippet(m: dict) -> str:
    """A short piece of the mention's own sentence around the matched word (for a document-wide search)."""
    ctx = norm(m.get("context") or m.get("passage") or "").lstrip("… ").rstrip(" …")
    i = ctx.lower().find((m["text"] or "").lower()) if m["text"] else -1
    if i < 0:
        return ctx[:60]
    return ctx[max(0, i - 30): i + len(m["text"]) + 30]


def locate(mentions: list[dict], paras: list[c.Para]) -> tuple[dict[int, dict[str, str]], int]:
    """Map each mention to a document paragraph. Returns ({pid: {company_id: name}}, unmapped_count).
    First on the page the pipeline reported (PDFs). If that fails (HTML filings: the pipeline's page numbers do not
    match the pages the viewer lays out), search the whole document for the mention's own sentence; when the same
    sentence appears several times, the mentions are assigned to its occurrences in page order."""
    by_page = collections.defaultdict(list)
    for p in paras:
        by_page[p.page].append(p)
    found: dict[int, dict[str, str]] = collections.defaultdict(dict)
    unmapped, later = 0, []
    for m in mentions:
        cands = by_page.get(m["page"], [])
        hit = None
        key = norm(m["passage"])[:40]
        if len(key) >= 20:
            hit = next((p for p in cands if key in norm(p.text)), None)
        if hit is None and m["text"]:
            hit = next((p for p in cands if has_word(p.text, m["text"])), None)
        if hit is None:
            later.append(m)
            continue
        found[hit.pid][m["company_id"]] = m["name"]
    used: dict[str, int] = collections.defaultdict(int)
    for m in sorted(later, key=lambda x: x["page"]):
        snip = _snippet(m).lower()
        hits = [p for p in paras if len(snip) >= 20 and snip in norm(p.text).lower()
                and (not m["text"] or has_word(p.text, m["text"]))]
        key = f"{m['company_id']}|{snip}"
        if hits:
            hit = hits[min(used[key], len(hits) - 1)]
            used[key] += 1
        else:
            # last resort: the sentence spans several short paragraphs (table cells, lists): take the paragraph that
            # contains the matched word, nearest to the page the pipeline reported
            word_hits = [p for p in paras if m["text"] and has_word(p.text, m["text"])]
            if not word_hits:
                unmapped += 1
                continue
            hit = min(word_hits, key=lambda p: (abs(p.page - m["page"]), p.pid))
        found[hit.pid][m["company_id"]] = m["name"]
    return found, unmapped


def looks_like_table(text: str) -> bool:
    chars = [ch for ch in text if not ch.isspace()]
    return bool(chars) and sum(ch.isdigit() or ch in "$%,." for ch in chars) / len(chars) > 0.35


def pick_filer(mentions: list[dict], override: str) -> tuple[str, str]:
    if override:
        for m in mentions:
            if override.lower() in (m["name"] or "").lower():
                return m["company_id"], m["name"]
        return "F0", override
    flagged = collections.Counter((m["company_id"], m["name"]) for m in mentions if m["is_filer"])
    if flagged:
        return flagged.most_common(1)[0][0]
    first = collections.Counter((m["company_id"], m["name"]) for m in mentions if m["page"] <= 3)
    return first.most_common(1)[0][0] if first else ("F0", "UNKNOWN FILER")


# ---------------------------------------------------------------- candidates and prompt
def unlisted_candidate(text: str) -> bool:
    """A paragraph with a business cue word and something that looks like a company name."""
    if not CUE_RX.search(text):
        return False
    if NAME_RX.search(text):
        return True
    return any(w not in ACRONYM_STOP for w in ACRONYM_RX.findall(text))


def build_units(paras, found, filer_id, skip_tables, listed_only) -> list[dict]:
    by_id = {p.pid: p for p in paras}
    units = []
    for p in paras:
        if skip_tables and looks_like_table(p.text):
            continue
        comps = {k: v for k, v in found.get(p.pid, {}).items() if k != filer_id}
        why = "listed" if comps else ""
        if not comps and not listed_only and len(p.text) >= 80 and unlisted_candidate(p.text):
            why = "cue"
        if not why:
            continue
        units.append({"pid": p.pid, "why": why, "comps": comps, "allowed": {f"C{k}" for k in comps}})
    targets = {u["pid"] for u in units}
    for u in units:
        lines = []
        pv, nx = by_id.get(u["pid"] - 1), by_id.get(u["pid"] + 1)
        u["ctx"] = ""
        if pv and pv.pid not in targets:
            lines.append(f"[ctx] {pv.text[:350]}")
            u["ctx"] += pv.text
        lines.append(by_id[u["pid"]].line())
        lines.append("COMPANIES: " + ("; ".join(f"C{k} {v}" for k, v in u["comps"].items()) or "(none listed)"))
        if nx and nx.pid not in targets:
            lines.append(f"[ctx] {nx.text[:350]}")
            u["ctx"] += " " + nx.text
        u["text"] = "\n".join(lines)
    return units


def make_chunks(units: list[dict], chunk_tokens: int) -> list[list[dict]]:
    chunks, cur, used = [], [], 0
    for u in units:
        t = c.est_tokens(u["text"])
        if cur and used + t > chunk_tokens:
            chunks.append(cur)
            cur, used = [], 0
        cur.append(u)
        used += t
    if cur:
        chunks.append(cur)
    return chunks


# ---------------------------------------------------------------- validation
def validate_row(r: dict, unit: dict | None, p: c.Para | None, fid: str, names: dict, aliases: dict,
                 listed_only: bool) -> tuple[str, str, str]:
    """Return (problem, from_display, to_display)."""
    if p is None or unit is None:
        return "paragraph was not a candidate", "", ""
    shown, others = [], []  # display names; non-filer endpoint names/aliases to find in the evidence
    for side in ("from", "to"):
        i, nm = str(r.get(f"{side}_id", "")), (r.get(f"{side}_name") or "").strip()
        if i == fid:
            shown.append(names.get(fid[1:], fid) if fid != "F0" else nm)
        elif i in unit["allowed"]:
            shown.append(names[i[1:]])
            others.append(aliases[i[1:]])
        elif i == "NEW" and not listed_only:
            if not nm or not has_word(p.text + " " + unit["ctx"], nm):
                return "unlisted company name is not in the text", "", ""
            shown.append(nm)
            others.append({nm})
        else:
            return "company id not allowed for this paragraph", "", ""
    if r["from_id"] == r["to_id"] and r["from_id"] != "NEW":
        return "from and to are the same company", "", ""
    if shown[0].lower() == shown[1].lower():
        return "from and to are the same company", "", ""
    ev = norm(r.get("evidence", ""))
    if not ev or ev not in norm(p.text + " " + unit["ctx"]):
        return "evidence quote is not in the paragraph", "", ""
    for alias_set in others:
        if not any(a and a.lower() in ev for a in alias_set):
            return "evidence does not name the other company", "", ""
    return "", shown[0], shown[1]


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--mentions", required=True, help="pipeline result for this PDF: *_mentions.csv or *_final.json")
    ap.add_argument("--filer", default="", help="part of the filer's name (default: the company flagged is_filer)")
    ap.add_argument("--listed-only", action="store_true", help="only companies from the pipeline result (no NEW ones)")
    ap.add_argument("--model", default=c.DEFAULT_MODEL)
    ap.add_argument("--chunk-tokens", type=int, default=4000)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--rpm", type=int, default=18)
    ap.add_argument("--max-tokens", type=int, default=8000)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--keep-tables", action="store_true", help="also send number-heavy table paragraphs")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tag", default="")
    a = ap.parse_args()

    pdf_name = Path(a.pdf).name
    paras = c.load_paragraphs(a.pdf)
    by_id = {p.pid: p for p in paras}
    mentions = load_mentions(a.mentions, pdf_name)
    if not mentions:
        raise SystemExit(f"no mentions for {pdf_name} in {a.mentions}")
    filer_id, filer_name = pick_filer(mentions, a.filer)
    fid = f"C{filer_id}" if filer_id != "F0" else "F0"
    found, unmapped = locate(mentions, paras)
    units = build_units(paras, found, filer_id, not a.keep_tables, a.listed_only)
    chunks = make_chunks(units, a.chunk_tokens)
    names = {m["company_id"]: m["name"] for m in mentions}
    names.setdefault(filer_id, filer_name)
    aliases: dict[str, set] = collections.defaultdict(set)
    for m in mentions:
        aliases[m["company_id"]] |= {m["name"] or "", m["text"] or ""}
    n_listed = sum(1 for u in units if u["why"] == "listed")
    tok = sum(c.est_tokens(u["text"]) for u in units)
    print(f"{pdf_name}: {len(mentions)} kept mentions of {len(names)} companies; filer = {filer_name} ({fid})")
    print(f"  mapped to paragraphs: {len(mentions) - unmapped}/{len(mentions)}")
    print(f"  candidate paragraphs: {len(units)} = {n_listed} with a listed company other than the filer"
          f" + {len(units) - n_listed} with business cue words and a company-like name"
          f"{' (--listed-only: cue search off)' if a.listed_only else ''}")
    print(f"  ~{tok:,} input tokens in {len(chunks)} chunks of ~{a.chunk_tokens} tokens")
    if a.dry_run:
        return 0

    system = SYSTEM.format(filer=filer_name, fid=fid, relations="\n".join(f"- {k}: {v}" for k, v in RELATIONS.items()),
                           unlisted_rule=UNLISTED_OFF if a.listed_only else UNLISTED_ON)
    limiter = c.RateLimiter(a.rpm)
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    t0 = time.monotonic()

    def run(i: int):
        text = "\n\n".join(u["text"] for u in chunks[i])
        res = c.call_llm(text, a.model, max_tokens=a.max_tokens, timeout=a.timeout, limiter=limiter,
                         label=f"chunk {i + 1}", system=system, schema=SCHEMA)
        rows, complete = c.parse_references(res.content)
        print(f"  chunk {i + 1}/{len(chunks)}: {len(chunks[i])} paragraphs -> {len(rows)} relations, {res.seconds:.0f}s, "
              f"${res.cost:.4f}{'' if complete and not res.error else '  <-- INCOMPLETE ' + res.error}"
              f"{f'  (429 x{res.rate_limit_hits})' if res.rate_limit_hits else ''}", flush=True)
        return res, rows, complete

    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        results = list(pool.map(run, range(len(chunks))))
    wall = time.monotonic() - t0
    unit_by_pid = {u["pid"]: u for u in units}

    raw, seen = [], set()
    for _, rows, _ in results:
        for r in rows:
            k = (r.get("para"), r.get("from_id"), r.get("from_name"), r.get("to_id"), r.get("to_name"), r.get("relation"))
            if k in seen:
                continue
            seen.add(k)
            p = by_id.get(r.get("para"))
            problem, fn, tn = validate_row(r, unit_by_pid.get(r.get("para")), p, fid, names, aliases, a.listed_only)
            r.update(valid=not problem, problem=problem, page=p.page if p else None, passage=p.text if p else "",
                     from_name=fn or r.get("from_name") or r.get("from_id"),
                     to_name=tn or r.get("to_name") or r.get("to_id"),
                     on_list=(r.get("from_id") != "NEW" and r.get("to_id") != "NEW"))
            raw.append(r)
    good = [r for r in raw if r["valid"]]

    def node_key(i: str, nm: str) -> str:
        return i if i != "NEW" else "NEW:" + nm.lower()

    edges: dict[tuple, dict] = {}
    for r in good:
        f, t = node_key(r["from_id"], r["from_name"]), node_key(r["to_id"], r["to_name"])
        fn, tn = r["from_name"], r["to_name"]
        if r["relation"] in SYMMETRIC and f > t:
            f, t, fn, tn = t, f, tn, fn
        e = edges.setdefault((f, t, r["relation"]), {
            "from_id": f, "to_id": t, "from_name": fn, "to_name": tn, "relation": r["relation"],
            "on_list": r["on_list"], "details": collections.Counter(), "paragraphs": [], "pages": set(),
            "confidences": [], "evidence": []})
        e["details"][r["detail"]] += 1
        e["paragraphs"].append(r["para"])
        e["pages"].add(r["page"])
        e["confidences"].append(r["confidence"])
        if len(e["evidence"]) < 3 and r["evidence"] not in e["evidence"]:
            e["evidence"].append(r["evidence"])
    edge_list = []
    for e in sorted(edges.values(), key=lambda x: -len(x["paragraphs"])):
        conf = e.pop("confidences")
        edge_list.append({**e, "details": [d for d, _ in e["details"].most_common(3)], "pages": sorted(e["pages"]),
                          "paragraphs": sorted(set(e["paragraphs"])), "mentions": len(e["paragraphs"]),
                          "max_confidence": max(conf), "avg_confidence": round(sum(conf) / len(conf))})

    nodes = [{"id": f"C{k}", "name": v, "on_list": True, "is_filer": k == filer_id,
              "kept_mentions": sum(1 for m in mentions if m["company_id"] == k),
              "relations": sum(1 for e in edge_list if f"C{k}" in (e["from_id"], e["to_id"]))} for k, v in names.items()]
    for e in edge_list:
        for i, nm in ((e["from_id"], e["from_name"]), (e["to_id"], e["to_name"])):
            if i.startswith("NEW:") and not any(n["id"] == i for n in nodes):
                nodes.append({"id": i, "name": nm, "on_list": False, "is_filer": False, "kept_mentions": 0,
                              "relations": sum(1 for x in edge_list if i in (x["from_id"], x["to_id"]))})
    failed = [i + 1 for i, (res, _, cm) in enumerate(results) if res.error or not cm]
    calls = [r for r, _, _ in results]
    invalid = collections.Counter(r["problem"] for r in raw if not r["valid"])
    meta = {
        "approach": "D_relations", "pdf": pdf_name, "model": a.model, "run_started_at": started,
        "mentions_file": a.mentions, "filer": filer_name, "listed_only": a.listed_only,
        "kept_mentions": len(mentions), "companies": len(names), "mentions_not_located": unmapped,
        "candidate_paragraphs": len(units), "candidates_listed": n_listed, "candidates_cue": len(units) - n_listed,
        "estimated_input_tokens": tok, "calls": len(chunks), "workers": a.workers, "max_tokens": a.max_tokens,
        "rpm_limit": a.rpm, "wall_seconds": round(wall, 1), "sum_call_seconds": round(sum(r.seconds for r in calls), 1),
        **c.totals(calls), "finish_reason": "ok" if not failed else f"chunks incomplete: {failed}",
        "output_complete": not failed, "error": "",
        "relation_rows": len(raw), "valid_rows": len(good), "invalid_rows": len(raw) - len(good),
        "invalid_reasons": dict(invalid),
        "edges": len(edge_list), "edges_on_list": sum(1 for e in edge_list if e["on_list"]),
        "edges_with_unlisted_company": sum(1 for e in edge_list if not e["on_list"]),
        "edges_with_filer": sum(1 for e in edge_list if fid in (e["from_id"], e["to_id"])),
        "edges_between_others": sum(1 for e in edge_list if fid not in (e["from_id"], e["to_id"])),
        "unlisted_companies": sum(1 for n in nodes if not n["on_list"]),
        "listed_companies_without_relation": [n["name"] for n in nodes if n["on_list"] and not n["is_filer"] and not n["relations"]],
        "relation_counts": dict(collections.Counter(e["relation"] for e in edge_list)),
    }
    c.OUT_DIR.mkdir(exist_ok=True)
    stem = f"D_{Path(a.pdf).stem}_{a.model.replace('/', '-')}{a.tag}"
    path = c.OUT_DIR / f"{stem}.json"
    path.write_text(json.dumps({
        "meta": meta, "calls": [c.call_record(r, len(rf), cm) for r, rf, cm in results],
        "nodes": nodes, "edges": edge_list, "rows": raw}, indent=1, ensure_ascii=False, default=list), encoding="utf-8")
    csv_path = c.OUT_DIR / f"{stem}_edges.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["from", "relation", "to", "on_list", "detail", "mentions", "pages", "max_confidence", "evidence",
                    "your_verdict"])
        for e in edge_list:
            w.writerow([e["from_name"], e["relation"], e["to_name"], "yes" if e["on_list"] else "NO (not on list)",
                        "; ".join(e["details"]), e["mentions"], ",".join(map(str, e["pages"])), e["max_confidence"],
                        " | ".join(e["evidence"]), ""])
    for k, v in meta.items():
        print(f"  {k:30} {v}")
    print("\ntop edges:")
    for e in edge_list[:15]:
        print(f"  {e['from_name'][:28]:28} --{e['relation']}--> {e['to_name'][:28]:28} x{e['mentions']} "
              f"conf {e['max_confidence']} pages {e['pages'][:4]}{'' if e['on_list'] else '  [not on list]'}")
    print(f"saved {path}\n      {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
