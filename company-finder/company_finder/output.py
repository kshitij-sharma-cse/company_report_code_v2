"""Summaries and writers (JSON, CSV, console table)."""
from __future__ import annotations

import csv
import json
import sys
from collections import Counter, OrderedDict
from pathlib import Path
from typing import TextIO

from .mention import Mention, mention_hits

# Fixed core columns for CSV/JSON mention rows. Future citation/table/metric
# fields go in ``extras`` (JSON) and optionally in ``EXTRA_FIELD_NAMES`` once
# promoted — do not emit a long wishlist of empty shell columns.
MENTION_FIELDS = [
    "company_id", "canonical_name", "matched_text", "match_type", "score",
    "document", "page", "page_number", "line_number", "location", "section",
    "is_filer", "note", "char_start", "char_end", "alias", "ambiguous",
    "context", "passage", "extras",
]

# Optional promoted extras (empty for Phase 1). Names listed here become extra
# CSV columns populated from ``mention.extras[name]`` when present.
EXTRA_FIELD_NAMES: list[str] = []


def csv_fieldnames() -> list[str]:
    """Core mention columns plus any promoted extras."""
    return list(MENTION_FIELDS) + list(EXTRA_FIELD_NAMES)


def _csv_row(m: dict) -> dict:
    """Prepare a mention dict for DictWriter (extras as JSON string)."""
    row = {k: m.get(k) for k in MENTION_FIELDS}
    extras = m.get("extras") or {}
    if not isinstance(extras, dict):
        extras = {}
    row["extras"] = json.dumps(extras, ensure_ascii=False, default=str)
    if row.get("page_number") is None:
        row["page_number"] = row.get("page")
    for k in EXTRA_FIELD_NAMES:
        row[k] = extras.get(k, "")
    return row


def summarise(mentions: list[Mention]) -> list[dict]:
    by: "OrderedDict[str, dict]" = OrderedDict()
    for m in mentions:
        s = by.get(m.company_id)
        if s is None:
            s = by[m.company_id] = {
                "company_id": m.company_id, "canonical_name": m.canonical_name, "count": 0,
                "match_types": Counter(), "matched_texts": Counter(), "documents": OrderedDict(),
                "sections": Counter(), "is_filer": False, "notes": Counter()}
        s["count"] += mention_hits(m)
        if m.section:
            s["sections"][m.section] += 1
        if m.note:
            s["notes"][m.note] += 1
        s["is_filer"] = s["is_filer"] or m.is_filer
        s["match_types"][m.match_type] += 1
        s["matched_texts"][m.matched_text] += 1
        d = s["documents"].setdefault(m.document or "<text>", {"count": 0, "pages": []})
        d["count"] += 1
        label = m.page if m.page is not None else m.location
        if label is not None and label not in d["pages"]:
            d["pages"].append(label)
    out = []
    for s in sorted(by.values(), key=lambda s: (-s["count"], s["canonical_name"])):
        out.append({
            "company_id": s["company_id"], "canonical_name": s["canonical_name"],
            "count": s["count"], "match_types": dict(s["match_types"]),
            "matched_texts": dict(s["matched_texts"].most_common()),
            "sections": dict(s["sections"].most_common()),
            "is_filer": s["is_filer"], "notes": dict(s["notes"].most_common()),
            "documents": [{"document": k, **v} for k, v in s["documents"].items()],
        })
    return out


def build_report(mentions: list[Mention], documents: list[dict], meta: dict) -> dict:
    return {"meta": meta, "summary": summarise(mentions), "documents": documents,
            "mentions": [m.to_dict() for m in mentions]}


def write_json(report: dict, fh: TextIO) -> None:
    json.dump(report, fh, ensure_ascii=False, indent=2, default=str)
    fh.write("\n")


# Primary CSV: one row per (company_id, document). Amit opens this file.
# ``context`` is the short snippet (restored in v1.4.5). ``passages`` is the
# full tagged list, cut to Excel's cell limit.
# v1.4.6 verification columns sit on the same row (do not drop them):
# matched surfaces, match types, best score, min score.
READABLE_FIELDS = [
    "company_id", "canonical_name", "document", "mention_count", "pages",
    "context", "passages",
    "matched_text", "match_type", "score", "min_score",
]

# Excel cell limit is 32,767 characters. Stay under it so a cell is not blank.
EXCEL_CELL_MAX = 30_000
_CELL_TRUNC_MARKER = "… (truncated; see *_mentions.csv)"
_PASSAGE_MORE = "… (+{n} more; see *_mentions.csv)"


def mentions_detail_path(out_path: str | Path) -> Path:
    """Per-hit sibling of the readable CSV: ``OUT.csv`` -> ``OUT_mentions.csv``."""
    dest = Path(out_path)
    return dest.with_name(dest.stem + "_mentions" + dest.suffix)


def _field(m, name, default=None):
    if isinstance(m, dict):
        return m.get(name, default)
    return getattr(m, name, default)


def _page_sort_key(page):
    try:
        return (0, int(page), "")
    except (TypeError, ValueError):
        return (1, 0, str(page))


def _passage_loc(m) -> str:
    """``p1 L12`` (``L`` omitted when line_number is missing)."""
    page = _field(m, "page_number", None)
    if page is None:
        page = _field(m, "page", None)
    line = _field(m, "line_number", None)
    bits = []
    if page is not None and str(page) != "":
        bits.append("p" + str(page))
    if line is not None and str(line) != "":
        bits.append("L" + str(line))
    return " ".join(bits)


def _passage_body(m) -> str:
    return str(_field(m, "passage", "") or _field(m, "context", "") or "").strip()


def _format_tagged(locs: list[str], body: str) -> str:
    if locs:
        prefix = "[" + "; ".join(locs) + "]"
        return (prefix + " " + body).strip() if body else prefix
    return body


def _tag_passage(m) -> str:
    """``[p1 L12] passage``. Omit ``L`` when line_number is missing."""
    loc = _passage_loc(m)
    return _format_tagged([loc] if loc else [], _passage_body(m))


def _fmt_score(score) -> str:
    """Compact score for the primary CSV. Empty when the group has none."""
    if score is None:
        return ""
    try:
        num = float(score)
    except (TypeError, ValueError):
        return _cap_plain(score)
    if num == int(num):
        return str(int(num))
    return str(num)


def _mention_score(m) -> float:
    raw = _field(m, "score", 0)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _cap_plain(text: str, limit: int = EXCEL_CELL_MAX) -> str:
    """Hard cap a non-passage cell. Marker is always present when cut."""
    text = "" if text is None else str(text)
    if len(text) <= limit:
        return text
    marker = _CELL_TRUNC_MARKER
    keep = limit - len(marker)
    if keep <= 0:
        return marker[:limit]
    return text[:keep] + marker


def _context_style(text: str) -> str:
    """One-line snippet, same length style as pre-merge ``context`` (~300)."""
    text = " ".join(str(text or "").split())
    if len(text) <= 300:
        return text
    return text[:299] + "…"


def fit_passages(lines: list[str], limit: int = EXCEL_CELL_MAX) -> str:
    """Join tagged passages with newlines, staying within ``limit``.

    Keeps as many *whole* lines as fit, then a final marker line
    ``… (+N more; see *_mentions.csv)``. Mid-line cut only when even one
    full line does not fit, and that cut still ends with the marker.
    """
    if not lines:
        return ""
    total = sum(len(x) for x in lines) + max(0, len(lines) - 1)
    if total <= limit:
        return "\n".join(lines)
    acc: list[str] = []
    acc_len = 0
    for k, line in enumerate(lines):
        n_more = len(lines) - (k + 1)
        marker = _PASSAGE_MORE.format(n=n_more)
        add = len(line) if k == 0 else 1 + len(line)
        cand_len = acc_len + add + 1 + len(marker)
        if cand_len <= limit:
            acc.append(line)
            acc_len += add
        else:
            break
    if acc:
        n_more = len(lines) - len(acc)
        return "\n".join(acc) + "\n" + _PASSAGE_MORE.format(n=n_more)
    # Zero full lines fit: prefix of the first line, then the marker.
    marker = _PASSAGE_MORE.format(n=len(lines))
    budget = limit - len(marker) - 1
    if budget <= 0:
        return marker[:limit]
    return lines[0][:budget] + "\n" + marker


def merge_readable(mentions: list) -> list[dict]:
    """One row per ``(company_id, document)``.

    ``mention_count`` counts hits. ``pages`` is sorted unique (``1,4,20``).
    ``context`` is the short snippet from the highest-score mention in the
    group (ties keep the earliest hit). If that snippet is blank, the earliest
    non-empty ``context`` is used, else a ~300-char one-line slice of the
    first passage. ``passages`` joins tagged blocks with newlines, not ``|``,
    and is capped at ``EXCEL_CELL_MAX`` characters.
    ``matched_text`` and ``match_type`` are the unique values in first-seen
    order, joined with `` | ``. ``score`` is the max in the group; ``min_score``
    is the min. Every primary cell is capped at ``EXCEL_CELL_MAX``.
    Different company ids (BLACKROCK vs Limited) stay separate rows.
    A paragraph is listed **once** per row: hits whose passage text is the
    same (whitespace-normalised) — name or alias, other line, other page —
    merge into one line tagged with every location, e.g.
    ``[p53 L47; p58 L5] Credit Default``. ``mention_count`` still counts
    every raw hit (``extras.hits_in_passage`` from :func:`dedupe_passages`).
    """
    groups: "OrderedDict[tuple, dict]" = OrderedDict()
    for m in mentions:
        cid = str(_field(m, "company_id", "") or "")
        doc = _field(m, "document", None) or ""
        key = (cid, str(doc))
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "company_id": cid,
                "canonical_name": _field(m, "canonical_name", "") or "",
                "document": str(doc),
                "mention_count": 0,
                "pages": [],
                "_page_seen": set(),
                "_pass": OrderedDict(),  # norm body -> {"body", "locs"}
                "_best_score": None,
                "_min_score": None,
                "_best_ctx": "",
                "_first_ctx": "",
                "_first_passage": "",
                "_texts": [],
                "_text_seen": set(),
                "_types": [],
                "_type_seen": set(),
            }
        g["mention_count"] += mention_hits(m)
        page = _field(m, "page_number", None)
        if page is None:
            page = _field(m, "page", None)
        if page is not None and str(page) != "" and page not in g["_page_seen"]:
            g["_page_seen"].add(page)
            g["pages"].append(page)
        ctx = str(_field(m, "context", "") or "")
        if ctx.strip() and not g["_first_ctx"]:
            g["_first_ctx"] = ctx
        passage_body = str(_field(m, "passage", "") or "")
        if passage_body.strip() and not g["_first_passage"]:
            g["_first_passage"] = passage_body
        score = _mention_score(m)
        if g["_best_score"] is None or score > g["_best_score"]:
            g["_best_score"] = score
            g["_best_ctx"] = ctx
        if g["_min_score"] is None or score < g["_min_score"]:
            g["_min_score"] = score
        surface = str(_field(m, "matched_text", "") or "")
        if surface and surface not in g["_text_seen"]:
            g["_text_seen"].add(surface)
            g["_texts"].append(surface)
        mtype = str(_field(m, "match_type", "") or "")
        if mtype and mtype not in g["_type_seen"]:
            g["_type_seen"].add(mtype)
            g["_types"].append(mtype)
        body = _passage_body(m)
        loc = _passage_loc(m)
        if body or loc:
            entry = g["_pass"].setdefault(" ".join(body.split()) if body else ("\0" + loc),
                                          {"body": body, "locs": []})
            if loc and loc not in entry["locs"]:
                entry["locs"].append(loc)
    rows = []
    for g in groups.values():
        pages = sorted(g["pages"], key=_page_sort_key)
        ctx = g["_best_ctx"]
        if not str(ctx).strip():
            ctx = g["_first_ctx"] or _context_style(g["_first_passage"])
        rows.append({
            "company_id": _cap_plain(g["company_id"]),
            "canonical_name": _cap_plain(g["canonical_name"]),
            "document": _cap_plain(g["document"]),
            "mention_count": g["mention_count"],
            "pages": _cap_plain(",".join(str(pg) for pg in pages)),
            "context": _cap_plain(ctx),
            "passages": fit_passages([_format_tagged(e["locs"], e["body"])
                                      for e in g["_pass"].values()]),
            "matched_text": _cap_plain(" | ".join(g["_texts"])),
            "match_type": _cap_plain(" | ".join(g["_types"])),
            "score": _fmt_score(g["_best_score"]),
            "min_score": _fmt_score(g["_min_score"]),
        })
    return rows


def _write_mention_rows(fh, mentions: list) -> None:
    w = csv.DictWriter(fh, fieldnames=csv_fieldnames(), extrasaction="ignore")
    w.writeheader()
    for m in mentions:
        row = m if isinstance(m, dict) else m.to_dict()
        w.writerow(_csv_row(row))


def write_csv(report: dict, out_path: str | None, *, mentions_detail: bool = True) -> list[str]:
    """Readable company x doc CSV (+ optional ``*_mentions.csv`` + ``*_summary.csv``).

    The file at ``out_path`` is the one to open: one row per company per
    document. ``context`` is one short snippet; ``passages`` lists tagged
    hits (page and line) and is truncated under the Excel cell limit.
    ``matched_text``, ``match_type``, ``score`` (best) and ``min_score`` are
    on that same row so a hit can be checked without opening the detail file.
    Per-hit evidence with the full ``context`` on every hit (after span
    dedupe, before this merge) goes to ``<stem>_mentions.csv`` unless
    ``mentions_detail`` is false. That detail file is not truncated.
    """
    written = []
    mentions = report.get("mentions") or []
    readable = merge_readable(mentions)
    if out_path:
        dest = Path(out_path)
        fh = open(dest, "w", newline="", encoding="utf-8")
    else:
        dest, fh = None, sys.stdout
    try:
        w = csv.DictWriter(fh, fieldnames=READABLE_FIELDS, extrasaction="ignore")
        w.writeheader()
        for row in readable:
            w.writerow(row)
    finally:
        if dest:
            fh.close()
            written.append(str(dest))
    if dest and mentions_detail:
        detail = mentions_detail_path(dest)
        with open(detail, "w", newline="", encoding="utf-8") as fh:
            _write_mention_rows(fh, mentions)
        written.append(str(detail))
    if dest:
        sp = dest.with_name(dest.stem + "_summary.csv")
        with open(sp, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["company_id", "canonical_name", "count", "match_types", "is_filer",
                        "sections", "documents_pages"])
            for s in report["summary"]:
                docs = "; ".join(f"{Path(d['document']).name}: {', '.join(map(str, d['pages']))}"
                                 for d in s["documents"])
                mt = ", ".join(f"{k}={v}" for k, v in s["match_types"].items())
                secs = "; ".join(f"{k} ({v})" for k, v in s.get("sections", {}).items())
                w.writerow([s["company_id"], s["canonical_name"], s["count"], mt,
                            s.get("is_filer", False), secs, docs])
        written.append(str(sp))
    return written


def dropped_path(out_path: str | Path) -> Path:
    """``OUT.csv`` -> ``OUT_dropped.csv`` (what prepare_targeted.py looks for)."""
    dest = Path(out_path)
    return dest.with_name(dest.stem + "_dropped.csv")


def write_dropped_csv(dropped: list[tuple[str, object]], out_path: str | Path) -> str:
    """Matches removed after matching, one row each: ``reason`` (``name_tail_rule``:
    tail of a longer name, see ``longer_name``; ``llm_disambiguate``: the model said
    it is not the company, see ``extras``) plus the usual mention columns.  Always
    written (header only when nothing was dropped) so an old file is not left behind."""
    dest = dropped_path(out_path)
    with open(dest, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["reason", "longer_name"] + csv_fieldnames(),
                           extrasaction="ignore")
        w.writeheader()
        for reason, m in dropped:
            row = _csv_row(m if isinstance(m, dict) else m.to_dict())
            extras = (m.get("extras") if isinstance(m, dict) else m.extras) or {}
            row.update(reason=reason, longer_name=extras.get("name_tail", ""))
            w.writerow(row)
    return str(dest)





def _trunc(s, n):
    s = "" if s is None else " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def format_table(report: dict, max_mentions: int = 40) -> str:
    lines = []
    summ = report["summary"]
    lines.append(f"Companies found: {len(summ)}   Mentions: {len(report['mentions'])}")
    if summ:
        lines.append("")
        hdr = f"{'Company':<36} {'Count':>5}  {'Types':<22} Pages / locations"
        lines += [hdr, "-" * len(hdr)]
        for s in summ:
            types = ",".join(f"{k}:{v}" for k, v in s["match_types"].items())
            locs = "; ".join(f"{Path(d['document']).name} {','.join(map(str, d['pages']))}"
                             for d in s["documents"])
            name = ("[FILER] " if s.get("is_filer") else "") + s["canonical_name"]
            lines.append(f"{_trunc(name, 36):<36} {s['count']:>5}  "
                         f"{_trunc(types, 22):<22} {_trunc(locs, 70)}")
    ms = report["mentions"]
    if ms:
        lines.append("")
        lines.append(f"Mentions (first {min(max_mentions, len(ms))} of {len(ms)}):")
        hdr = (f"{'Company':<24} {'Matched':<24} {'Type':<6} {'Score':>5} {'Where':<26} "
               f"{'Section':<24} Context / note")
        lines += [hdr, "-" * len(hdr)]
        for m in ms[:max_mentions]:
            where = f"{m.get('location') or ''} {Path(m['document']).name if m.get('document') else ''}".strip()
            name = ("*" if m.get("is_filer") else "") + m["canonical_name"]
            extra = f"  [{m['note']}]" if m.get("note") else ""
            lines.append(f"{_trunc(name, 24):<24} {_trunc(m['matched_text'], 24):<24} "
                         f"{m['match_type']:<6} {m['score']:>5.0f} {_trunc(where, 26):<26} "
                         f"{_trunc(m.get('section') or '', 24):<24} "
                         f"{_trunc(m['context'], 90)}{extra}")
        if any(m.get("is_filer") for m in ms):
            lines.append("(* = the document's own filer/issuer)")
    for d in report.get("documents", []):
        for w in d.get("warnings") or []:
            lines.append(f"WARNING {Path(d['path']).name}: {w}")
        if d.get("error"):
            lines.append(f"ERROR   {Path(d['path']).name}: {d['error']}")
    return "\n".join(lines)
