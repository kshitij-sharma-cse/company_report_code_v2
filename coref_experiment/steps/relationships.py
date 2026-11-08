"""Step 2: relationships between the companies in the passed list (supplier, customer, manufacturer, any kind).

Scope     Only the companies in the pipeline result passed with --mentions. No other company is ever an endpoint.
Method    1. Candidates are the PDF paragraphs where a kept mention of a listed company other than the filer sits
             (page by page), with the neighbouring paragraphs as context. The filer is implicit ("we/our/the Company").
          2. One decision per company: "what is <company> to the filer?" (a role from a fixed list, or none), and one
             decision per pair of listed companies in the same paragraph. The model does NOT choose a direction: the
             role is turned into a "from -> relation -> to" edge by code, which avoids reversed edges.
          3. Every answer needs a verbatim quote from the paragraph that names the company; otherwise it is rejected.
          4. Identical paragraphs (repeated risk-factor text) are asked once and the answer is reused on every page.
Outputs   steps/out/<pdf>_relationships.csv        one row per relationship found, page by page
          steps/out/<pdf>_relationship_items.csv   every decision, including "none" and rejected answers (audit)
          steps/out/<pdf>_relationships_run.json   settings, counts, tokens, cost, time, rate limits

Usage:
  company-finder/.venv/bin/python coref_experiment/steps/relationships.py PDF --mentions final.json
        [--model openai/gpt-4.1] [--min-confidence 50] [--dry-run]
"""

from __future__ import annotations

import argparse
import collections
import csv
import difflib
import itertools
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import shared as s

c = s.c
d = s.d

# role of X relative to Y  ->  edge (from, relation, to)   [X and Y are placeholders]
ROLE_EDGE = {
    "supplier": ("X", "supplies", "Y"),
    "customer": ("Y", "supplies", "X"),
    "manufacturer": ("X", "manufactures_for", "Y"),
    "service_provider": ("X", "provides_services_to", "Y"),
    "licensor": ("X", "licenses_to", "Y"),
    "licensee": ("Y", "licenses_to", "X"),
    "distributor": ("X", "distributes_for", "Y"),
    "investor": ("X", "invests_in", "Y"),
    "portfolio_company": ("Y", "invests_in", "X"),
    "lender": ("X", "lends_to", "Y"),
    "borrower": ("Y", "lends_to", "X"),
    "acquirer": ("X", "acquired", "Y"),
    "acquired_company": ("Y", "acquired", "X"),
    "parent": ("Y", "subsidiary_of", "X"),
    "subsidiary": ("X", "subsidiary_of", "Y"),
    "underwriter": ("X", "underwrites_for", "Y"),
    "adviser": ("X", "advises", "Y"),
    "partner": ("X", "partners_with", "Y"),
    "competitor": ("X", "competes_with", "Y"),
    "other": ("X", "other", "Y"),
}
ROLE_HELP = {
    "supplier": "X supplies goods, components, materials, equipment or software to Y",
    "customer": "X buys products or services from Y (X is Y's customer)",
    "manufacturer": "X makes, fabricates or assembles products for Y (a foundry or contract manufacturer)",
    "service_provider": "X provides services to Y (testing, hosting, logistics, transfer agent, consulting...)",
    "licensor": "X licenses technology, IP or software to Y",
    "licensee": "X licenses technology, IP or software from Y",
    "distributor": "X distributes or resells Y's products",
    "investor": "X owns shares in, or invests in, Y",
    "portfolio_company": "Y owns shares in, or invests in, X",
    "lender": "X lends money to, or finances, Y",
    "borrower": "X borrows money from Y",
    "acquirer": "X acquired or merged with Y",
    "acquired_company": "Y acquired or merged with X",
    "parent": "X owns Y (Y is X's subsidiary)",
    "subsidiary": "X is a subsidiary or affiliate of Y",
    "underwriter": "X underwrites or places Y's securities",
    "adviser": "X advises Y as auditor, lawyer, agent or consultant",
    "partner": "the text says X and Y collaborate, form an alliance or develop something together (only when it says so)",
    "related_unclear": "the text links X and Y in business but does not say how, or in which direction (for example \"agreements with third parties such as X and Y\"); do NOT guess a role or a direction",
    "competitor": "X and Y compete",
    "other": "any other business relationship (describe it in detail)",
}
ROLES = list(ROLE_EDGE) + ["related_unclear", "none"]

# Direction check. The model also names the company that DOES the action ("actor"). For a directed role the code
# knows which of X / Y should be the actor; when the model's role and its actor disagree on a lending relation, the actor
# wins and the edge is flipped (role "lender" for X = Acme while the actor is the fund: the fund lends to Acme). For any other
# role the disagreement marks the row "unclear" (a tenant of a mortgaged property is not a supplier of the trust); the report
# leaves such rows out of its edges.
ACTOR_HELP = {
    "supplier": "the supplier", "customer": "the supplier", "manufacturer": "the manufacturer",
    "service_provider": "the service provider", "licensor": "the licensor", "licensee": "the licensor",
    "distributor": "the distributor", "investor": "the investor (the owner of the shares)",
    "portfolio_company": "the investor (the owner of the shares)", "lender": "the lender",
    "borrower": "the lender", "acquirer": "the acquirer (the buyer)", "acquired_company": "the acquirer (the buyer)",
    "parent": "the parent (the owner)", "subsidiary": "the parent (the owner)", "underwriter": "the underwriter",
    "adviser": "the adviser",
}
ACTOR_IS_X = {"supplier", "manufacturer", "service_provider", "licensor", "distributor", "investor", "lender",
              "acquirer", "parent", "underwriter", "adviser"}   # for the other directed roles the actor is Y
INVERSE_ROLE = {"supplier": "customer", "licensor": "licensee", "investor": "portfolio_company", "lender": "borrower",
                "acquirer": "acquired_company", "parent": "subsidiary"}
INVERSE_ROLE.update({v: k for k, v in list(INVERSE_ROLE.items())})

SYSTEM = """You find BUSINESS RELATIONSHIPS between companies in paragraphs of a financial/legal document
(prospectus, annual report, Form C, fund filing...).

FILER: {filer} (id {fid}). The filer is the company that submits the document. In its own text "we", "our", "us",
"the Company" (and sometimes "it") mean the filer.
{filing}

Each paragraph is shown as:
  [P<n>|p<page>] text
  ITEMS: i<k>: X = <id> <name>, Y = <id> <name> ; ...     <- the questions to answer for this paragraph
Lines starting with [ctx] are neighbouring paragraphs, given only to resolve pronouns and lists.

For EVERY item answer: what is X to Y? Choose exactly one role (X is the ... of Y):
{roles}
  none: no business relationship between X and Y is stated or clearly implied in the text.

Rules:
- Answer from the text only. A company that is merely listed or named (a list of competitors' products, an
  index, a signature, an address, a trademark line, a name in a quotation) is "none" unless the text says what it
  does for or with Y. If the text says X competes with Y, the role is "competitor".
- POSITIONS ARE NOT RELATIONSHIPS. A person's job or board seat (director, board member, chairman, CEO, CFO, president,
  officer, executive, employee, former employee) says nothing about the companies. "Dr. Zhang is Executive Chairman of
  Crossbar and a director of the filer" does NOT make Crossbar a relation of the filer: answer none. Ignore such
  sentences, unless the same sentence also states a company-to-company relationship ("... until it was acquired by B").
- A product or technology name is not a company.
- Think about direction by asking the question "X is the <role> of Y": 
   for "we buy wafers from TSMC" TSMC (X) is the supplier of the filer (Y); 
   for "our customers include Whoop" Whoop (X) is a customer of the filer (Y); 
   for "distributors such as F" F (X) is the distributor of the filer (Y).
- NEVER GUESS A DIRECTION OR A TYPE. If the text links X and Y but does not say who does what for whom, or what kind of
  relationship it is (for example "licensing arrangements with third parties such as A and B": who licenses to whom is
  not stated), answer related_unclear with a confidence of at most 60. Use "partner" only when the text itself says they
  collaborate, form an alliance or develop something together.
- actor: the id (exactly as shown, e.g. C0001806928 or F0) of the company that DOES the action of the role:
  {actors}.
  Example: "the Fund purchased an investment in Acme Inc." -> the investor is the Fund, so actor = the Fund's id, and
  the role of X = Acme is portfolio_company. Check that role and actor agree. For partner, competitor, other,
  related_unclear and none the actor is "".
- evidence: a short VERBATIM quote (max 25 words) from the paragraph that supports the role and contains the name
  of X (or its short form); empty only when the role is "none".
- detail: what is supplied, licensed or done, max 8 words ("wafer fabrication"); empty if none.
- reason comes first: one short sentence, then the role.
- confidence: integer 0-100; at most 60 when the direction or type is not explicit.
Return one row for EVERY item: {{"references": [{{"item_no": k, "reason": ..., "role": ..., "actor": ..., "detail": ...,
"evidence": ..., "confidence": ...}}]}}."""

SCHEMA = {
    "name": "roles",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["references"],
        "properties": {
            "references": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "item_no",
                        "reason",
                        "role",
                        "actor",
                        "detail",
                        "evidence",
                        "confidence",
                    ],
                    "properties": {
                        "item_no": {"type": "integer"},
                        "reason": {"type": "string"},
                        "role": {"type": "string", "enum": ROLES},
                        "actor": {"type": "string"},
                        "detail": {"type": "string"},
                        "evidence": {"type": "string"},
                        "confidence": {"type": "integer"},
                    },
                },
            }
        },
    },
}

REL_COLS = [
    "page",
    "paragraph_no",
    "from_company",
    "relation",
    "to_company",
    "role",
    "role_sentence",
    "detail",
    "confidence",
    "reason",
    "evidence_quote",
    "direction_check",
    "context",
]
UNCLEAR_COLS = ["page", "paragraph_no", "company_x", "company_y", "confidence", "reason", "evidence_quote", "context"]
ITEM_COLS = [
    "item",
    "page",
    "paragraph_no",
    "x_company",
    "y_company",
    "role",
    "status",
    "problem",
    "confidence",
    "reason",
    "evidence_quote",
    "reused_from_page",
]


def quote_in_text(ev: str, text: str) -> int:
    """Position of the quote in the text (normalised), or -1. A quote may skip words with '...': every piece must
    appear in the text, in order (pieces of fewer than 3 words are ignored)."""
    nt = d.norm(text)
    pieces = [d.norm(x) for x in re.split(r"\.{2,}|…", ev)]
    pieces = [x for x in pieces if len(x.split()) >= 3] or [d.norm(ev)]
    pos, first = 0, -1
    for x in pieces:
        i = nt.find(x, pos)
        if i < 0:
            return fuzzy_quote(ev, nt)
        first = i if first < 0 else first
        pos = i + len(x)
    return first


def fuzzy_quote(ev: str, nt: str, min_cover: float = 0.85, min_run: int = 4) -> int:
    """Models sometimes drop or merge a few words when quoting. Accept the quote when at least 85% of its words
    appear in the text in the same order, with a run of at least 4 consecutive words. Returns the start position or -1.
    """
    qw = [w for w in re.findall(r"\S+", d.norm(re.sub(r"\.{2,}|…", " ", ev)))]
    spans = [(m.group(0), m.start()) for m in re.finditer(r"\S+", nt)]
    tw = [w for w, _ in spans]
    if len(qw) < 5:
        return -1
    blocks = [
        b
        for b in difflib.SequenceMatcher(
            None, tw, qw, autojunk=False
        ).get_matching_blocks()
        if b.size
    ]
    cover = sum(b.size for b in blocks) / len(qw)
    if cover >= min_cover and max(b.size for b in blocks) >= min_run:
        return spans[blocks[0].a][1]
    return -1


def edge_for(role: str, x: str, y: str) -> tuple[str, str, str]:
    m = {"X": x, "Y": y}
    f, rel, t = ROLE_EDGE[role]
    return m[f], rel, m[t]


def check_direction(role: str, actor: str, x: str, y: str, ids: dict) -> tuple[str, str, str, str]:
    """(edge from, relation, edge to, check note) after comparing the role with the actor the model named.
    ids maps the ids shown to the model (C..., F0) and the names to the companies x / y."""
    f, rel, t = edge_for(role, x, y)
    if role not in ACTOR_HELP:
        return f, rel, t, "undirected"
    who = ids.get((actor or "").strip()) or ids.get((actor or "").strip().lower())
    if who not in (x, y):
        return f, rel, t, "not checked (no actor)"
    expected = x if role in ACTOR_IS_X else y
    if who == expected:
        return f, rel, t, "ok"
    if role not in ("lender", "borrower"):   # only a lending relation is flipped; otherwise the text does not say who does what
        return f, rel, t, f"unclear: role '{role}' disagreed with actor {who}"
    return t, rel, f, f"flipped: role '{role}' disagreed with actor {who}"


def build_items(scope: s.Scope, skip_tables: bool) -> list[dict]:
    fid = scope.filer_id
    items = []
    for pid in sorted(scope.found):
        p = scope.by_id[pid]
        comps = [k for k in scope.found[pid] if k != fid]
        if not comps or (skip_tables and d.looks_like_table(p.text)):
            continue
        for x in comps:
            items.append({"pid": pid, "x": x, "y": fid})
        for a, z in itertools.combinations(comps, 2):
            items.append({"pid": pid, "x": a, "y": z})
    return items


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument(
        "--mentions",
        required=True,
        help="pipeline result: final.json or *_mentions.csv",
    )
    ap.add_argument("--filer", default="")
    ap.add_argument("--form", default="", help="SEC form type (D, 10-K, 10-Q ...): told to the model")
    ap.add_argument("--filing-date", default="", help="filing date, e.g. 2026-04-15")
    ap.add_argument("--model", default=c.DEFAULT_MODEL)
    ap.add_argument("--min-confidence", type=int, default=50)
    ap.add_argument("--chunk-tokens", type=int, default=4000)
    ap.add_argument(
        "--retry-rounds",
        type=int,
        default=2,
        help="how many times questions the model did not answer are asked again (one paragraph per call)",
    )
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--rpm", type=int, default=18)
    ap.add_argument("--max-tokens", type=int, default=8000)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--keep-tables", action="store_true")
    ap.add_argument(
        "--out-dir", default="", help="where to write the outputs (default: steps/out)"
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tag", default="")
    a = ap.parse_args()

    if a.out_dir:
        s.OUT_DIR = Path(a.out_dir)
    scope = s.load_scope(a.pdf, a.mentions, a.filer)
    fid = scope.filer_id
    items = build_items(scope, not a.keep_tables)
    # identical paragraph text + same pair = one question, answer reused on every page
    uniq: dict[tuple, dict] = {}
    for it in items:
        k = (d.norm(scope.by_id[it["pid"]].text), it["x"], it["y"])
        it["key"] = k
        uniq.setdefault(k, it)
    roles_txt = "\n".join(f"  {r}: {h}" for r, h in ROLE_HELP.items())
    actors_txt = "; ".join(f"{r} -> {h}" for r, h in ACTOR_HELP.items())
    filing = s.filing_context(a.form, a.filing_date, scope.filer_name, scope.filer_id != "F0")
    system = SYSTEM.format(filer=scope.filer_name, fid=scope.cid(fid), roles=roles_txt, actors=actors_txt, filing=filing)
    ids: dict = {}   # what the model may answer as actor -> company id
    for k, nm in scope.names.items():
        ids[scope.cid(k)] = k
        ids[(nm or "").lower()] = k
    limiter = c.RateLimiter(a.rpm)

    def make_chunks(pending: list[dict], chunk_tokens: int) -> list[dict]:
        """Group questions by paragraph; number them; cut into chunks of about chunk_tokens."""
        by_pid: dict[int, list] = collections.OrderedDict()
        for it in pending:
            by_pid.setdefault(it["pid"], []).append(it)
        chunks, cur, used, n = [], {"text": [], "items": {}}, 0, 0
        for pid, lst in by_pid.items():
            p = scope.by_id[pid]
            before, after = s.context_lines(scope, pid, before=1, after=1)
            qs = []
            for it in lst:
                n += 1
                it["no"] = n
                qs.append(
                    f"i{n}: X = {scope.cid(it['x'])} {scope.names[it['x']]}, Y = "
                    f"{scope.cid(it['y'])} {scope.names[it['y']]}{' (the filer)' if it['y'] == fid else ''}"
                )
            block = "\n".join(
                x for x in (before, p.line(), "ITEMS: " + " ; ".join(qs), after) if x
            )
            t = c.est_tokens(block)
            if cur["items"] and used + t > chunk_tokens:
                chunks.append(cur)
                cur, used = {"text": [], "items": {}}, 0
            cur["text"].append(block)
            for it in lst:
                cur["items"][it["no"]] = it
            used += t
        if cur["items"]:
            chunks.append(cur)
        return chunks

    first_chunks = make_chunks(list(uniq.values()), a.chunk_tokens)
    tok = sum(c.est_tokens(t) for ch in first_chunks for t in ch["text"])
    print(
        f"{Path(a.pdf).name}: filer = {scope.filer_name}; {len(scope.names)} listed companies; "
        f"{len({it['pid'] for it in items})} candidate paragraphs (page by page)"
    )
    print(
        f"  {len(items)} questions ({sum(1 for it in items if it['y'] == fid)} company-vs-filer, "
        f"{sum(1 for it in items if it['y'] != fid)} company pairs); {len(uniq)} after reusing identical paragraphs"
    )
    print(
        f"  ~{tok:,} input tokens in {len(first_chunks)} chunks (model {a.model}); unanswered questions are re-asked "
        f"up to {a.retry_rounds} times, one paragraph per call"
    )
    if a.dry_run:
        return 0

    started = time.strftime("%Y-%m-%d %H:%M:%S")
    t0 = time.monotonic()
    all_results: list = []  # (CallResult, rows, complete) over all rounds
    verdict: dict[tuple, dict] = {}

    def run_round(chunks: list[dict], tag: str) -> None:
        def run(i: int):
            ch = chunks[i]
            n_items = len(ch["items"])
            head = (
                f"There are {n_items} items in this request (i{min(ch['items'])} to i{max(ch['items'])}). "
                f"Answer ALL {n_items}, one row each, including the ones whose answer is none.\n\n"
            )
            res = c.call_llm(
                head + "\n\n".join(ch["text"]),
                a.model,
                max_tokens=a.max_tokens,
                timeout=a.timeout,
                limiter=limiter,
                label=f"{tag} chunk {i + 1}",
                system=system,
                schema=SCHEMA,
            )
            out, complete = c.parse_references(res.content)
            print(
                f"  {tag} chunk {i + 1}/{len(chunks)}: {n_items} questions -> {len(out)} answers, {res.seconds:.0f}s, "
                f"${res.cost:.4f}{'' if complete and not res.error else '  <-- INCOMPLETE ' + res.error}"
                f"{f'  (429 x{res.rate_limit_hits})' if res.rate_limit_hits else ''}",
                flush=True,
            )
            return res, out, complete

        with ThreadPoolExecutor(max_workers=a.workers) as pool:
            results = list(pool.map(run, range(len(chunks))))
        all_results.extend(results)
        for (res, out, _), ch in zip(results, chunks):
            for r in out:
                it = ch["items"].get(r.get("item_no"))
                if not it or it["key"] in verdict:
                    continue
                verdict[it["key"]] = judge(it, r)

    def judge(it: dict, r: dict) -> dict:
        p = scope.by_id[it["pid"]]
        before, after = s.context_lines(scope, it["pid"], before=1, after=1, cut=100000)
        text = " ".join([before, p.text, after])
        role, ev, conf = (
            r.get("role", "none"),
            (r.get("evidence") or "").strip(),
            r.get("confidence", 0),
        )
        problem = ""
        if role not in ROLE_EDGE and role not in ("none", "related_unclear"):
            problem = "unknown role"
        elif role in ("other", "adviser", "partner") and s.is_position_text(ev):
            problem = "a position or job is not a relationship between companies"
        elif role != "none":
            pos = quote_in_text(ev, text) if ev else -1
            if pos < 0:
                problem = "evidence quote is not in the paragraph"
            else:
                # each non-filer company must be named in the quote or in the sentence the quote comes from
                sent = s.sentence_at(d.norm(text), pos)
                for company in [it["x"]] + ([it["y"]] if it["y"] != fid else []):
                    if not any(
                        d.has_word(ev, al) or d.has_word(sent, al)
                        for al in scope.aliases.get(company, set())
                        if al
                    ):
                        problem = f"neither the quote nor its sentence names {scope.names[company]}"
                        break
        status = (
            "rejected"
            if problem
            else (
                "none"
                if role == "none"
                else (
                    "unclear"
                    if role == "related_unclear"
                    else ("low_confidence" if conf < a.min_confidence else "relationship")
                )
            )
        )
        return {
            "role": role,
            "status": status,
            "problem": problem,
            "conf": conf,
            "reason": r.get("reason", ""),
            "detail": r.get("detail", ""),
            "evidence": ev,
            "actor": r.get("actor", ""),
            "from_page": p.page,
        }

    run_round(first_chunks, "round 0")
    for rnd in range(1, a.retry_rounds + 1):
        pending = [it for k, it in uniq.items() if k not in verdict]
        if not pending:
            break
        print(
            f"  {len(pending)} questions unanswered: asking again, one paragraph per call (round {rnd})"
        )
        run_round(make_chunks(pending, 1), f"round {rnd}")
    results = all_results
    chunks = first_chunks
    wall = time.monotonic() - t0

    # ---- expand to every page, write outputs
    rel_rows, item_rows, unclear_rows = [], [], []
    for k, it in enumerate(items, 1):
        p = scope.by_id[it["pid"]]
        v = verdict.get(it["key"])
        x_name, y_name = scope.names[it["x"]], scope.names[it["y"]]
        row = {
            "item": k,
            "page": p.page,
            "paragraph_no": p.pid,
            "x_company": x_name,
            "y_company": y_name,
            "role": v["role"] if v else "",
            "status": v["status"] if v else "no_answer",
            "problem": v["problem"] if v else "the model returned no answer",
            "confidence": v["conf"] if v else "",
            "reason": v["reason"] if v else "",
            "evidence_quote": v["evidence"] if v else "",
            "reused_from_page": (
                v["from_page"] if v and v["from_page"] != p.page else ""
            ),
        }
        item_rows.append(row)
        if v and v["status"] == "unclear":
            unclear_rows.append(
                {
                    "page": p.page,
                    "paragraph_no": p.pid,
                    "company_x": x_name,
                    "company_y": y_name,
                    "confidence": v["conf"],
                    "reason": v["reason"],
                    "evidence_quote": v["evidence"],
                    "context": p.text[:3000],
                }
            )
        if v and v["status"] == "relationship":
            fid_, rel, tid_, check = check_direction(v["role"], v["actor"], it["x"], it["y"], ids)
            role = v["role"]
            if check.startswith("flipped"):
                role = INVERSE_ROLE.get(role, role)
            # the sentence always reads "<from> ... <to>" in the corrected direction
            if role in INVERSE_ROLE or not check.startswith("flipped"):
                sentence = f"{x_name} is the {role.replace('_', ' ')} of {y_name}"
            else:   # no inverse role name (manufacturer, adviser ...): say it the other way round
                sentence = f"{y_name} is the {role.replace('_', ' ')} of {x_name}"
            rel_rows.append(
                {
                    "page": p.page,
                    "paragraph_no": p.pid,
                    "from_company": scope.names[fid_],
                    "relation": rel,
                    "to_company": scope.names[tid_],
                    "role": role,
                    "role_sentence": sentence,
                    "detail": v["detail"],
                    "confidence": v["conf"],
                    "reason": v["reason"],
                    "evidence_quote": v["evidence"],
                    "direction_check": check,
                    "context": p.text[:3000],
                }
            )
    s.OUT_DIR.mkdir(exist_ok=True)
    stem = f"{Path(a.pdf).stem}{a.tag}"
    with open(
        s.OUT_DIR / f"{stem}_relationships.csv", "w", newline="", encoding="utf-8"
    ) as fh:
        w = csv.DictWriter(fh, fieldnames=REL_COLS)
        w.writeheader()
        w.writerows(rel_rows)
    with open(
        s.OUT_DIR / f"{stem}_relationships_review.csv", "w", newline="", encoding="utf-8"
    ) as fh:  # linked in the text, but how / in which direction is not stated: for a person to decide
        w = csv.DictWriter(fh, fieldnames=UNCLEAR_COLS)
        w.writeheader()
        w.writerows(unclear_rows)
    with open(
        s.OUT_DIR / f"{stem}_relationship_items.csv", "w", newline="", encoding="utf-8"
    ) as fh:
        w = csv.DictWriter(fh, fieldnames=ITEM_COLS)
        w.writeheader()
        w.writerows(item_rows)
    calls = [r for r, _, _ in results]
    failed = [i + 1 for i, (res, _, cm) in enumerate(results) if res.error or not cm]
    st = collections.Counter(r["status"] for r in item_rows)
    uniq_edges = {(r["from_company"], r["relation"], r["to_company"]) for r in rel_rows}
    meta = {
        "step": "relationships",
        "pdf": Path(a.pdf).name,
        "model": a.model,
        "run_started_at": started,
        "mentions_file": a.mentions,
        "filer": scope.filer_name,
        "form": a.form,
        "filing_date": a.filing_date,
        "listed_companies": len(scope.names),
        "min_confidence": a.min_confidence,
        "candidate_paragraphs": len({it["pid"] for it in items}),
        "questions": len(items),
        "unique_questions": len(uniq),
        "calls": len(results),
        "wall_seconds": round(wall, 1),
        **c.totals(calls),
        "finish_reason": "ok" if not failed else f"chunks incomplete: {failed}",
        "output_complete": not failed,
        "by_status": dict(st),
        "relationship_rows": len(rel_rows),
        "unclear_rows": len(unclear_rows),
        "unique_edges": len(uniq_edges),
        "relation_counts": dict(collections.Counter(r["relation"] for r in rel_rows)),
    }
    (s.OUT_DIR / f"{stem}_relationships_run.json").write_text(
        json.dumps(
            {
                "meta": meta,
                "calls": [c.call_record(r, len(o), cm) for r, o, cm in results],
            },
            indent=1,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    for k, v in meta.items():
        print(f"  {k:24} {v}")
    print("\nrelationships:")
    for e in sorted(uniq_edges):
        print(f"  {e[0][:30]:30} --{e[1]}--> {e[2][:30]}")
    print(
        f"saved {s.OUT_DIR / (stem + '_relationships.csv')}\n      {s.OUT_DIR / (stem + '_relationship_items.csv')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
