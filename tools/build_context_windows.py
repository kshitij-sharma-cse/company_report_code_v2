#!/usr/bin/env python3
"""Context-window workbook for the recall check (evidence_recall.py): sheet "Windows", one row per window of text around the
company's name in each downloaded filing.

    python tools/build_context_windows.py databricks --out out/databricks_context_windows.xlsx
    python tools/build_context_windows.py databricks --folders databricks databricks_ncsr --dumps DIR --context 50

A line is one paragraph, table row or PDF line. A window holds up to --context lines either side of each mention; nearby
mentions share a window. Mention lines start with >>> and the name is wrapped in « » (what evidence_recall.py looks for).
The name pattern is the config's company.name_regex, word-bounded. Columns: Window ID, Form, Filing date, Company (filer),
Accession number, File name, Mention lines, Line range, LLM input (metadata + window text).
"""
import argparse
import csv
import html
import json
import re
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parents[2]
CONFIG = Path(__file__).resolve().parents[1] / "coref_experiment" / "company_report" / "model_config"
CELL = 32000                                         # Excel holds 32,767 characters a cell
BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")    # characters Excel files cannot hold


def dump_folders(dumps: Path, co: str, cfg: dict) -> list:
    """The company's dump folders: config dump_folders, else <co> and every <co>_<word> that has a manifest (e.g. _ncsr, _delta)."""
    if cfg.get("dump_folders"):
        return [f for f in cfg["dump_folders"] if (dumps / f"{f}_manifest.csv").exists()]
    extra = sorted(p.name[:-len("_manifest.csv")] for p in dumps.glob(f"{co}_*_manifest.csv"))
    return [f for f in [co] + [x for x in extra if re.fullmatch(rf"{re.escape(co)}_[A-Za-z0-9]+", x)]
            if (dumps / f"{f}_manifest.csv").exists() and (dumps / f).is_dir()]


def lines(p: Path, pat=None) -> list:
    """One line per paragraph / table row (HTML, XML), source line (plain text) or PDF line. HTML treats a source newline as a
    space, so markup text is collapsed first and only block tags break lines; a name wrapped in plain text or PDF is rejoined."""
    if p.suffix.lower() == ".pdf":
        import fitz
        with fitz.open(p) as d:
            return rejoin([re.sub(r"\s+", " ", x).strip() for pg in d for x in pg.get_text().split("\n") if x.strip()], pat)
    t = p.read_text(encoding="utf-8", errors="ignore")
    if p.suffix.lower() not in (".htm", ".html", ".xml") and not re.search(r"(?i)<(html|body|table|div|p)\b", t[:20000]):
        return rejoin([re.sub(r"\s+", " ", x).strip() for x in t.split("\n") if x.strip(" |\n\t\r\xa0")], pat)   # plain text
    t = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", t)
    t = re.sub(r"\s+", " ", t)                      # markup: a source newline is only a space
    t = re.sub(r"(?i)</(tr|p|div|li|h\d|table)>|<br\s*/?>", "\n", t)
    t = re.sub(r"(?i)</t[dh]>", " | ", t)
    if p.suffix.lower() == ".xml":                   # structured XML: one element a line
        t = re.sub(r"</[^>]+>", "\n", t)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    return [re.sub(r"\s+", " ", x).strip(" |") for x in t.split("\n") if x.strip(" |\n\t\r\xa0")]


def rejoin(ls: list, pat) -> list:
    """Hard-wrapped text: a name cut over two lines ('Get' / 'Heal Inc.') joins them, so the mention is one line."""
    if pat is None:
        return ls
    out, i = [], 0
    while i < len(ls):
        if i + 1 < len(ls) and not pat.search(ls[i]) and not pat.search(ls[i + 1]) and pat.search(ls[i] + " " + ls[i + 1]):
            out.append(ls[i] + " " + ls[i + 1])
            i += 2
        else:
            out.append(ls[i])
            i += 1
    return out


def spans(hits: list, n: int, k: int) -> list:
    out = []
    for i in hits:                                   # merge nearby mentions into one window
        lo, hi = max(0, i - k), min(n, i + k + 1)
        if out and lo <= out[-1][1]:
            out[-1][1] = hi
        else:
            out.append([lo, hi])
    return out


def render(ls: list, lo: int, hi: int, pat) -> str:
    body = []
    for i in range(lo, hi):
        x = ls[i]
        if pat.search(x):
            x = ">>> " + pat.sub(lambda g: f"«{g[0]}»", x)
        body.append(f"L{i + 1}| {x}")
    return "\n".join(body)


def windows(ls: list, hits: list, pat, k: int, meta_len: int) -> list:
    """Spans whose text fits one cell; a span that does not is cut again with half the context (mentions are never dropped)."""
    out = []
    for lo, hi in spans(hits, len(ls), k):
        txt = render(ls, lo, hi, pat)
        inside = [i for i in hits if lo <= i < hi]
        if len(txt) + meta_len <= CELL:
            out.append((lo, hi, inside, txt))
        elif k == 0 and len(inside) == 1:            # one line longer than a cell: cut around its first mention
            head, m = re.match(r"L\d+\| (?:>>> )?", txt)[0], re.search(r"«", txt)
            cut = max(len(head), m.start() - CELL // 2) if m else len(head)
            out.append((lo, hi, inside, (head + txt[cut:])[:CELL - meta_len]))
        elif k > 0:
            out += windows(ls, inside, pat, k // 2, meta_len)
        else:                                       # mentions on adjacent lines, still too long: one window each
            out += [w for i in inside for w in windows(ls, [i], pat, 0, meta_len)]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("company")
    ap.add_argument("--config", default="")
    ap.add_argument("--dumps", default=str(ROOT / "dumps"))
    ap.add_argument("--folders", nargs="*", default=None, help="dump folders (default: config dump_folders, else <co> and <co>_<word>)")
    ap.add_argument("--regex", default="", help="name pattern (default: config company.name_regex)")
    ap.add_argument("--context", type=int, default=50, help="lines either side of a mention")
    ap.add_argument("--out", default="", help="default: <dumps>/<co>_context_windows.xlsx")
    a = ap.parse_args()
    cfgp = Path(a.config) if a.config else CONFIG / f"{a.company}.json"
    cfg = json.loads(cfgp.read_text(encoding="utf-8")) if cfgp.exists() else {}
    rx = a.regex or cfg.get("company", {}).get("name_regex", "")
    if not rx:
        raise SystemExit(f"no name pattern: set company.name_regex in {cfgp} or pass --regex")
    pat = re.compile(rf"\b(?:{rx})\b", re.I)
    dumps = Path(a.dumps)
    folders = a.folders or dump_folders(dumps, a.company, cfg)
    if not folders:
        raise SystemExit(f"no dump folders with a manifest for {a.company} under {dumps}")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Windows"
    ws.append(["Window ID", "Form", "Filing date", "Company (filer)", "Accession number", "File name", "Mention lines", "Line range",
               "LLM input (metadata + window text)"])
    n, files, missing, errors = 0, 0, 0, []
    for folder in folders:
        for m in csv.DictReader(open(dumps / f"{folder}_manifest.csv", encoding="utf-8")):
            p = dumps / folder / m["file"]
            if not p.exists():
                missing += 1
                continue
            try:
                ls = lines(p, pat)
            except Exception as e:                   # an unreadable file is reported, the rest still run
                errors.append(f"{m['file']}: {type(e).__name__}: {e}"[:200])
                continue
            hits = [i for i, x in enumerate(ls) if pat.search(x)]
            files += bool(hits)
            meta = f"Form: {m['form']}\nFiler: {m['filer']}\nFiling date: {m['file_date']}\n--- TEXT ---\n"
            for lo, hi, inside, txt in windows(ls, hits, pat, a.context, len(meta)):
                n += 1
                ws.append([f"W{n:06d}", m["form"], m["file_date"], re.sub(r"\s*\(.*", "", m["filer"]), m["accession"], Path(m["file"]).name,
                           " ".join(f"L{i + 1}" for i in inside), f"L{lo + 1}–L{hi}", BAD.sub(" ", meta + txt)])
    out = Path(a.out) if a.out else dumps / f"{a.company}_context_windows.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    print(json.dumps({"folders": folders, "windows": n, "files_with_mentions": files, "files_missing": missing, "errors": errors[:5],
                      "pattern": pat.pattern, "out": str(out)}))


if __name__ == "__main__":
    main()
