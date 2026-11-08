#!/usr/bin/env python3
"""Recall test: every number on a line naming the company in the context-window workbook must be in the evidence rows or be listed. Sentences near a mention that say 'the company' with a money figure are listed too."""
import collections, csv, re, sys
from pathlib import Path
import openpyxl
csv.field_size_limit(10**8)
co, book = sys.argv[1], sys.argv[2]
ev = Path(__file__).parent / "out" / co / "evidence"
E = collections.defaultdict(set)
tiers = collections.defaultdict(set)
for r in csv.DictReader(open(ev / "evidence_rows.csv", encoding="utf-8")):
    for k in ("qty", "cost", "value", "commitment"):
        if r.get(k):
            E[r["accession"]].add(round(float(r[k]), 2))
    E[r["accession"]].update(float(x.replace(",", "")) for x in re.findall(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", r["raw"]))
NUM = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:billion|million|thousand|[bmk]\b))?|\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b", re.I)
wb = openpyxl.load_workbook(book, read_only=True, data_only=True)
w = wb["Windows"]
rows = w.iter_rows(values_only=True)
h = list(next(rows))
ix = {k: h.index(k) for k in ("Window ID", "Form", "Filing date", "Company (filer)", "Accession number", "LLM input (metadata + window text)")}
seen, lines, miss, coref = set(), 0, [], []
acc = collections.Counter()
for r in rows:
    if r[ix["Window ID"]] is None:
        continue
    a = r[ix["Accession number"]]
    txt = r[ix["LLM input (metadata + window text)"]] or ""
    ls = txt.split("--- TEXT ---", 1)[-1].split("\n")
    acc[a] += 1
    for i, ln in enumerate(ls):
        body = re.sub(r"^\s*(>>>)?\s*L\d+\|\s*(>>>)?", "", ln).replace("«", "").replace("»", "").strip()
        if re.search(r"«\s*data\s?bricks", ln, re.I):
            k = (a, body[:200])
            if k in seen:
                continue
            seen.add(k)
            nums = [m[0] for m in NUM.finditer(body)]
            nums = [n for n in nums if not re.fullmatch(r"\$?\s?(19|20)\d\d", n)]
            if not nums:
                continue
            lines += 1
            vals = [float(re.sub(r"[^\d.]", "", n) or 0) for n in nums]
            if not any(v in E.get(a, ()) for v in vals):
                miss.append([r[ix["Form"]], str(r[ix["Filing date"]])[:10], r[ix["Company (filer)"]], a, "name line", body[:360]])
        elif re.search(r"\b(the company|its|it)\b.{0,90}\$\s?\d", ln, re.I) and re.search(r"revenue|run-rate|run rate|valuation|funding|raised|financing|debt", ln, re.I):
            near = ls[max(0, i - 3):i + 1]
            if any(re.search(r"«\s*data\s?bricks", x, re.I) for x in near):
                k = (a, body[:200])
                if k not in seen:
                    seen.add(k)
                    coref.append([r[ix["Form"]], str(r[ix["Filing date"]])[:10], r[ix["Company (filer)"]], a, "'the company' line", body[:360]])
with open(ev / "recall_list.csv", "w", newline="", encoding="utf-8") as fh:
    x = csv.writer(fh); x.writerow(["form", "filed", "filer", "accession", "kind", "line"]); x.writerows(miss + coref)
held = sum(1 for a in acc if a in E)
print({"windows_filings": len(acc), "filings_with_evidence_rows": held, "name_lines_with_numbers": lines, "not_in_evidence": len(miss), "company_lines_to_read": len(coref)})
print(collections.Counter(m[0] for m in miss).most_common(10))
