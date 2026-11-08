"""Shared pieces for the two steps (indirect mentions, relationships).

Scope rule: only the companies in the pipeline result passed in (final.json or *_mentions.csv) are used.
The PDF is read with the same paragraph splitting as the earlier experiments (common.load_paragraphs).
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # coref_experiment/ : common.py, approach_d_relations.py

import approach_d_relations as d  # noqa: E402  (load_mentions, locate, pick_filer, looks_like_table, norm, has_word)
import common as c  # noqa: E402

OUT_DIR = HERE / "out"
c.MIN_PARA_CHARS = 1  # keep tiny text blocks (headings like 'Our Mission') so every trigger word is accounted for

# ------------------------------------------------------------------ trigger words (indirect mentions)
TRIGGER_RX = {
    "first_person": re.compile(r"\b(we|our|ours|us|ourselves)\b", re.I),
    "third_singular": re.compile(r"\b(it|its|itself)\b", re.I),
    "third_plural": re.compile(r"\b(they|their|theirs|them|themselves)\b", re.I),
    # a defined term is capitalised ("the Company"); lower-case "the corporation" is generic legal text
    "defined_term": re.compile(r"\b[Tt]he (Company|Fund|Issuer|Registrant|Partnership|Trust|Corporation)\b"),
}
FILER_RULE_TYPES = {"first_person", "defined_term"}   # resolved by rule to the filer
LLM_TYPES = {"third_singular", "third_plural"}        # need the text to decide


def find_triggers(text: str) -> list[dict]:
    """Every trigger word in `text`: {type, word, start, end}. All-capital tokens (US, IT, WE in headings) are skipped."""
    out = []
    for ttype, rx in TRIGGER_RX.items():
        for m in rx.finditer(text):
            w = m.group(0)
            if len(w) > 1 and w.isupper():
                continue
            out.append({"type": ttype, "word": w, "start": m.start(), "end": m.end()})
    return sorted(out, key=lambda t: t["start"])


SENT_SPLIT = re.compile(r"(?<=[.!?;])\s+(?=[A-Z“\"(•])")


def sentence_at(text: str, pos: int) -> str:
    start = 0
    for m in SENT_SPLIT.finditer(text):
        if m.end() > pos:
            return text[start:m.start() + 1].strip()
        start = m.end()
    return text[start:].strip()


# ------------------------------------------------------------------ positions are not company relationships
# A person's job or board seat ("Dr. Zhang is Executive Chairman of Crossbar", "sits on the boards of A and B") says
# nothing about a business relationship between two companies. Used as a safety net behind the prompts.
POSITION_RX = re.compile(
    r"\b(boards?|board members?|directors?|chair(?:man|woman|person)?|executive chair\w*|"
    r"chief [a-z]+ officers?|CEO|CFO|COO|CTO|CIO|CMO|presidents?|vice presidents?|officers?|"
    r"served as|has served|sits on|serves on|member of (?:the |our |its )?board)\b", re.I)


def is_position_text(text: str) -> bool:
    return bool(POSITION_RX.search(text or ""))


# ------------------------------------------------------------------ what kind of filing this is
# A private company files about itself on Form D (and Reg CF Form C, Reg A forms): there the filer IS the company, and we /
# our / the Company mean it. Annual and quarterly reports (10-K, 10-Q, 8-K, 20-F, proxy ...), registration statements and
# fund forms are filed by PUBLIC companies and funds: for a private company on the list, the filer of such a form is a
# different company, so we / our / the Company in it never mean a listed company.
PRIVATE_ISSUER_FORMS = {"D", "D/A", "C", "C/A", "C-U", "C-AR", "C-AR/A", "C-TR", "1-A", "1-A/A", "1-K", "1-SA", "1-U"}


def clean_form(form: str) -> str:
    f = re.sub(r"^(sec )?form[\s_-]*", "", (form or "").strip(), flags=re.I).upper().replace("_", "/")
    return f.replace("10-K-A", "10-K/A").replace("N-2-A", "N-2/A").replace("S-1-A", "S-1/A")


def is_private_issuer_form(form: str) -> bool:
    return clean_form(form) in PRIVATE_ISSUER_FORMS


def filing_context(form: str, date: str, filer_name: str, filer_listed: bool) -> str:
    """A short statement for the prompts: which form, which date, who files it, and what that means for we / our / the Company."""
    f, year = clean_form(form), (re.search(r"(19|20)\d\d", date or "") or [""])[0] if date else ""
    head = []
    if f:
        head.append(f"Form {f}")
    if date:
        head.append(f"filed {date}")
    what = " ".join(head) if head else "an SEC filing"
    time_note = (f" Read every statement as of the filing date ({date}); a company's size, owners and deals can be very different in "
                 "other years.") if date else ""
    fn_end = filer_name if filer_name.endswith(".") else filer_name + "."
    if filer_listed:
        if is_private_issuer_form(f):
            who = (f"This document is {what}, a form a private company files about itself. The filer, {filer_name}, is one of the "
                   f"LISTED companies, so \"we\", \"our\", \"us\", \"the Company\" mean {fn_end}")
        else:
            who = (f"This document is {what}. The filer, {filer_name}, is one of the LISTED companies, so \"we\", \"our\", \"us\" and "
                   f"\"the Company\" in the filer's own text mean {fn_end}")
    else:
        pub = ("" if is_private_issuer_form(f) or not f else
               f" Form {f} is filed by public companies and funds, while the listed companies are private companies, so the filer is "
               "never a listed company.")
        who = (f"This document is {what}. The filer is {filer_name}, which is NOT one of the listed companies.{pub} "
               f"\"We\", \"our\", \"us\", \"the Company\" and \"it\" for the filer mean {filer_name}, never a listed company, unless the "
               "document itself defines that word as one of the listed companies (for example \"the Target\"). A listed company is "
               "meant only where the text names it or a term that clearly stands for it.")
    return who + time_note


# ------------------------------------------------------------------ scope
@dataclass
class Scope:
    pdf: str
    paras: list
    by_id: dict
    filer_id: str                    # e.g. "376-ambiq" (pipeline company id)
    filer_name: str
    names: dict                      # company id -> display name (all listed companies)
    aliases: dict                    # company id -> set of strings seen in the text
    found: dict                      # paragraph id -> {company id: name} (direct mentions)
    mentions: list = field(default_factory=list)
    unmapped: int = 0

    def cid(self, company_id: str) -> str:
        return company_id if company_id == "F0" else f"C{company_id}"


def load_scope(pdf: str, mentions_path: str, filer_override: str = "") -> Scope:
    paras = c.load_paragraphs(pdf)
    mentions = d.load_mentions(mentions_path, Path(pdf).name)
    if not mentions:
        raise SystemExit(f"no mentions for {Path(pdf).name} in {mentions_path}")
    filer_id, filer_name = d.pick_filer(mentions, filer_override)
    found, unmapped = d.locate(mentions, paras)
    names = {m["company_id"]: m["name"] for m in mentions}
    names.setdefault(filer_id, filer_name)
    aliases: dict = {}
    for m in mentions:
        aliases.setdefault(m["company_id"], set()).update({m["name"] or "", m["text"] or ""})
    return Scope(pdf, paras, {p.pid: p for p in paras}, filer_id, filer_name, names, aliases, found, mentions, unmapped)


def listed_near(scope: Scope, pid: int, window: int) -> dict:
    """Listed companies mentioned directly in paragraphs pid-window .. pid+window ({id: name})."""
    out: dict = {}
    for q in range(pid - window, pid + window + 1):
        out.update(scope.found.get(q, {}))
    return out


def context_lines(scope: Scope, pid: int, before: int = 2, after: int = 1, cut: int = 350) -> tuple[str, str]:
    """(text of previous paragraphs, text of next paragraphs) as [ctx] lines, for resolving pronouns."""
    prev = [f"[ctx] {scope.by_id[q].text[:cut]}" for q in range(pid - before, pid) if q in scope.by_id]
    nxt = [f"[ctx] {scope.by_id[q].text[:cut]}" for q in range(pid + 1, pid + after + 1) if q in scope.by_id]
    return "\n".join(prev), "\n".join(nxt)
