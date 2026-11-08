#!/usr/bin/env python3
"""Read every number by rule, with no model: iXBRL tags, then HTML tables by their printed column headers, then fixed text layouts.
One row per source cell group. Every row keeps the filing, table, row, printed header, printed unit and raw cells. A row that cannot be tied to a column and a unit goes to review, never into evidence.

    python evidence_tables.py databricks --folders databricks databricks_ncsr databricks_delta
"""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import html
import json
import multiprocessing as mp
import re
from pathlib import Path

csv.field_size_limit(10**8)
HERE = Path(__file__).parent
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
MONTH = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
COL = [("commitment", r"unfunded|commitment|undrawn"), ("price", r"\bper\s+(?:share|unit)\b"), ("value", r"\b(fair|market)?\s*values?\b"), ("cost", r"\bcost\b"), ("qty", r"\bshares\b|\bprincipal\b|\bpar\b|\bunits\b|\bquantity\b|\bface\b|\bnumber of\b"), ("date", r"\bdate\b|\bacquired\b"), ("pct", r"%|percent")]
HEAD_UNIT = re.compile(r"\(?\$?\s*000[’'`]?s?\)?|thousands|millions|\$\s?m\b", re.I)
PAGE_UNIT = re.compile(r"(?:\bin\s|\$\s|expressed in\s|amounts?\s|value\s|cost\s)[^.<]{0,40}?(thousands|millions|\$\s?000[’'`]?s?)\b", re.I)
CACHE_V = "layout-v7"     # bump when a reader changes, so cached rows of older code are not reused
NUMBER = re.compile(r"^\(?\$?\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*\)?\s*[*†‡#]*\s*(?:\([a-z0-9]{1,3}\)\s*,?\s*)*[a-z]{0,1}$")
SHARES_TXT = re.compile(r"(?<![\d,.$])(\d{1,3}(?:,\d{3})+|\d{3,})\s+(?:[A-Za-z][\w.-]*\s+){0,5}?shares?\b", re.I)


def text(x: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", x)).replace("\xa0", " ").replace("​", " ").replace("‌", "")).strip()


def grid(tr: str) -> list:
    """Cells of a row with their column position, colspans expanded."""
    res, col = [], 0
    for attrs, body in re.findall(r"<t[dh]\b([^>]*)>(.*?)</t[dh]>", tr, re.S | re.I):
        m = re.search(r"colspan\s*=\s*[\"']?(\d+)", attrs, re.I)
        span = int(m[1]) if m else 1
        t = text(body)
        if t:
            res.append((col, col + span - 1, t))
        col += span
    return res


def head_row(tr: str, key: str = ""):
    """The cells of a header row, or None. A header row names a value or a cost column, holds no figure in a cell of its own (a year or a unit is not
    one) and does not name the company."""
    if not re.search(r"value|cost", tr, re.I):
        return None
    g = grid(tr)
    joined = " ".join(c[2] for c in g)
    if not g or len(joined) >= 240 or not re.search(r"\bvalues?\b|\bcost\b", joined, re.I) or re.search(r"\d,\d{3}", joined) or (key and re.search(key, joined, re.I)):
        return None
    if any(re.fullmatch(r"\(?\$?\s*[\d,]*\.?\d+\s*%?\s*\)?", c[2]) and not re.fullmatch(r"(?:19|20)\d{2}|\(?\$?\s*000[’'`]?s?\)?", c[2]) for c in g):     # a figure alone, not 'Preferred securities 1.47%'
        return None
    return g


def header_of(rows: list, key: str = "") -> list:
    """The printed header nearest above the rows' end, as (first col, last col, label) cells, or nothing: one <table> can print several tables, each with
    its own header. Label rows printed just above or below it ('Shares' and '(000)' under 'Value', 'of net' / 'assets' under 'Percent'), back to the
    header before, join the label they sit under. A label naming a column of its own ('Shares') joins only a label whose span holds it and that names no
    other kind, else stands alone; a fragment of no kind that touches several labels and sits inside none is left out; a unit alone joins only a cost,
    value or commitment label."""
    idx = [i for i, tr in enumerate(rows) if head_row(tr, key)]
    if not idx:
        return []
    k = len(idx) - 1
    while k > 0 and all(not grid(tr) for tr in rows[idx[k - 1] + 1:idx[k]]):     # header rows printed one under another are one header
        k -= 1
    i, lo = idx[k], (idx[k - 1] + 1 if k > 0 else 0)
    labels = [list(c) for c in head_row(rows[i], key)]
    for step in (-1, 1):
        j, n = i + step, 0
        while lo <= j < len(rows) and n < 4:
            h = grid(rows[j])
            j += step
            if not h:
                continue
            if not label_row(h, key):
                break
            n += 1
            for c in h:
                kind = col_of(c[2])
                money = not kind and unit_of(c[2])[0]      # a unit alone ('(000)') joins only a money label, never shares or the name
                over = [(min(c[1], l[1]) - max(c[0], l[0]), -x) for x, l in enumerate(labels) if not money or col_of(l[2]) in ("cost", "value", "commitment")]
                if kind:
                    over = [o for o in over if labels[-o[1]][0] <= c[0] and c[1] <= labels[-o[1]][1] and col_of(labels[-o[1]][2]) in (None, kind)]
                elif not money and sum(o >= 0 for o, _ in over) > 1 and not any(labels[-x][0] <= c[0] and c[1] <= labels[-x][1] for _, x in over):
                    continue
                o, x = max(over, default=(-1, 0))
                if o >= 0:
                    labels[-x][2] = f"{c[2]} {labels[-x][2]}" if step < 0 else f"{labels[-x][2]} {c[2]}"
                else:
                    labels.append(list(c))
    return sorted(tuple(x) for x in labels)


def label_row(g: list, key: str = "") -> bool:
    """A further row of a printed header: short labels, no amount, not the company; one cell alone must name a column or a unit, or go on a label."""
    for c in g:
        s = re.sub(DATE + r"|\d{1,2}/\d{1,2}/\d{2,4}|\(?\$?\s*000[’'`]?s?\)?|\b(?:19|20)\d{2}\b|\(\s*[a-z0-9]{1,2}\s*\)", " ", c[2])
        if len(c[2]) > 40 or re.search(r"\d", s) or (key and re.search(key, c[2], re.I)):
            return False
    return len(g) > 1 or bool(col_of(g[0][2]) or unit_of(g[0][2])[0] or re.match(r"[a-z]", g[0][2]))     # 'of net': a label wrapped onto the next line


def col_of(label: str):
    for name, rx in COL:
        if re.search(rx, label, re.I):
            return name
    return None


def unit_of(label: str):
    if re.search(r"million|\$\s?m\b", label, re.I):
        return 1e6, label
    if re.search(r"\b000|thousand", label, re.I):
        return 1e3, label
    return None, ""


def page_unit(t: str, pos: int):
    """The nearest unit statement above the table, back to the start of its schedule."""
    z = t[max(0, pos - 60000):pos]
    h = max((m.start() for m in re.finditer(r"schedules? of investments|portfolio of investments|statements? of investments", z, re.I)), default=0)
    sl = text(z[max(0, h - 1500):])
    hits = list(PAGE_UNIT.finditer(sl))
    if not hits:
        return None, ""
    m = hits[-1]
    return (1e6 if "million" in m[1].lower() else 1e3), sl[max(0, m.start() - 20):m.end() + 10]


SP = r"(?:\s|&nbsp;|&#160;|&#xa0;|<[^>]{0,300}>)+"     # space, or tags, between the words of a heading in raw html
DATE = r"(?i:" + MONTH + r")\.?\s+\d{1,2}\s*,\s*\d{4}"
PRE = r"(?:(?:condensed|consolidated|combined|unaudited)"      # words that can open a statement heading
HEAD_RAW = re.compile(PRE + SP + r")*(?:schedules?|portfolios?|statements?)" + SP + r"of" + SP + r"(?:portfolio" + SP + r")?investments|>\s*investments\b|quarterly" + SP + r"holdings" + SP + r"report|investment" + SP + r"portfolio\b", re.I)
END_RAW = re.compile(PRE + SP + r")*(?:statements?" + SP + r"of" + SP + r"(?:assets" + SP + r"and" + SP + r"liabilities|operations|changes" + SP + r"in" + SP + r"net" + SP + r"assets|cash" + SP + r"flows)|financial" + SP + r"highlights|notes" + SP + r"to" + SP + r"(?:(?:the|unaudited|condensed|consolidated|combined)" + SP + r")*financial" + SP + r"statements)", re.I)
AT_START = re.compile(r">(?:\s|&nbsp;|&#160;|&#xa0;)*$")     # a heading opens its element; prose and footnotes do not
GAP = r"(?:\s*(?:\([^()]{0,90}\)|[-–—:,]|\b(?:as of|as at|at|continued|unaudited)\b))*\s*"
W = r"(?!" + DATE + r")(?-i:[A-Z0-9][\w&'’®™℠.,/-]*)\s+"
FUND = r"(?P<fund>" + W + r"(?:(?:" + W + r"|(?-i:of|and|the|for|in|&|[-–—])\s+)){0,15}?)?"     # a fund name between heading and date: capitalised words, never prose, never a date
HEAD_TXT = re.compile(r"^(?:(?:condensed|consolidated|combined|unaudited)\s+)*(?:(?:schedules?|portfolios?|statements?)\s+of\s+(?:portfolio\s+)?investments|investment\s+portfolio)\b" + GAP + FUND + GAP + r"(" + DATE + r")|^investments\b" + GAP + r"(" + DATE + r")|^quarterly\s+holdings\s+report\b[^.\d]{0,160}?(" + DATE + r")", re.I)
NOT_HEAD = re.compile(r"\s*(?:\([^()]{0,40}\)\s*)?(?:" + DATE + r"|and\s+(?:" + DATE + r"|\d{4}))|\s*,\s*[a-z]")     # after the date: a second date (comparison columns) or prose going on
FOOTNOTE = re.compile(r"\([a-z0-9]{1,3}\)\s*(?:at|as\s+of|as\s+at)\s+$", re.I)     # '(h) At March 31, 2024, the Fund held ...'
AS_OF_RAW = re.compile(r">\s*(?:\(unaudited\)\s*)?as" + SP + r"(?:of|at)" + SP + r"(" + MONTH + r"(?:\s|&nbsp;|&#160;)+\d{1,2},(?:\s|&nbsp;|&#160;)*\d{4})(?:\s|&nbsp;|&#160;)*(?:\(unaudited\))?\s*<", re.I)
NUM_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})\b")


def norm_date(s: str) -> str:
    """'DECEMBER 31,2016' -> 'December 31, 2016'; '' when it is not a real date."""
    import datetime as dt
    m = re.search(r"(" + MONTH + r")\.?\s+(\d{1,2})\s*,\s*(\d{4})", re.sub(r"\s+", " ", s), re.I)
    try:
        return dt.datetime.strptime(f"{m[1].title()} {m[2]} {m[3]}", "%B %d %Y").strftime("%B ") + f"{int(m[2])}, {m[3]}" if m else ""
    except ValueError:
        return ""


def period_of(t: str) -> str:
    """The filing's own period of report: the EDGAR header of a .txt submission, the 'Date of reporting period:' line of an N-CSR/N-Q cover, else the iXBRL dei tag. Empty when not printed."""
    m = re.search(r"CONFORMED PERIOD OF REPORT:\s*(\d{4})(\d{2})(\d{2})", t[:20000])
    if m:
        return norm_date(f"{MONTHS[int(m[2]) - 1]} {int(m[3])}, {m[1]}") if 1 <= int(m[2]) <= 12 else ""
    c = re.search(r"date\s+of\s+reporting\s+period\s*:?\s*(.{0,80})", text(t[:60000]), re.I)
    if c:
        z = re.split(r"\bitem\b", c[1], flags=re.I)[0]      # the span ends at the next item of the form
        ds = [(x.start(), norm_date(x[0])) for x in re.finditer(DATE, z)] + [(x.start(), num_date(x)) for x in NUM_DATE.finditer(z)]
        ds = [d for d in sorted(ds) if d[1]]
        if ds:
            return ds[-1][1]     # 'October 1, 2023 - March 31, 2024': the period ends on the last date
    m = re.search(r'name="dei:DocumentPeriodEndDate"[^>]*>', t, re.I)
    if not m:
        return ""
    v = text(t[m.end():m.end() + 800])[:40]      # the tag can hold a nested tag, so read the text after it
    return iso_long(v) if re.match(r"\d{4}-", v) else norm_date(v)


def num_date(m) -> str:
    """'06/30/2026' -> 'June 30, 2026' (US order); '' when it is not a real date."""
    mo, d, y = int(m[1]), int(m[2]), int(m[3]) + (2000 if len(m[3]) == 2 else 0)
    return norm_date(f"{MONTHS[mo - 1]} {d}, {y}") if 1 <= mo <= 12 else ""


def ends_of(t: str) -> list:
    """Positions of the statement and notes headings that end a schedule (assets and liabilities, operations, highlights, notes)."""
    return [m.start() for m in END_RAW.finditer(t) if m[0][0].isupper() and AT_START.search(t[max(0, m.start() - 80):m.start()])]


def schedule_dates(t: str, filed: str = "") -> list:
    """(position, date, end) of every dated schedule heading: '[Consolidated] Schedule/Portfolio/Statement of Investments <date>', 'Investments <date>',
    'Quarterly Holdings Report for <fund> <date>', a caption cell 'As of <date>'. A multi-fund filing gives one per fund. A heading must open its element;
    one dated after the filing, followed by a second date (columns of a comparison), by prose, or led by a footnote marker is not a schedule date.
    A heading dates rows up to the next statement or notes heading (assets and liabilities, operations, highlights, notes), so notes tables after the last schedule are not given its date."""
    out = []
    for m in HEAD_RAW.finditer(t):
        if m[0][0] != ">" and not AT_START.search(t[max(0, m.start() - 80):m.start()]):
            continue
        s = text(t[m.start() + (m[0][0] == ">"):m.start() + 3000])[:320]
        h = HEAD_TXT.match(s)
        if h and not NOT_HEAD.match(s[h.end():]) and not FOOTNOTE.search(s[:h.start(h.lastindex)]):
            out.append((m.start(), norm_date(h[h.lastindex])))
    ends = ends_of(t)
    import bisect
    out = [(p, d, next((e for e in ends[bisect.bisect_right(ends, p):]), len(t))) for p, d in out]
    for m in AS_OF_RAW.finditer(t):     # a caption 'As of <date>' dates only its own table, or the table below it
        e = t.find("</table>", m.start())
        out.append((m.start(), norm_date(m[1]), e if e >= 0 else m.start() + 20000))
    import datetime as dt
    ok = lambda d: d and (not filed or dt.datetime.strptime(d, "%B %d, %Y").date().isoformat() <= filed)
    return sorted(x for x in out if ok(x[1]))


def from_ixbrl(t: str, key: str) -> list:
    ctx = {}
    for cid, b in re.findall(r'<xbrli:context id="([^"]+)">(.*?)</xbrli:context>', t, re.S):
        m = re.search(r"InvestmentIdentifierAxis\.domain>(.*?)</", b, re.S)
        if m and re.search(key, m[1], re.I):
            inst = re.search(r"<xbrli:instant>(.*?)<", b)
            if inst:
                ctx[cid] = (html.unescape(m[1]), inst[1])
    rows: dict = {}
    for attrs, val in re.findall(r"<ix:nonFraction([^>]*)>(.*?)</ix:nonFraction>", t, re.S) if ctx else []:
        c = re.search(r'contextRef="([^"]+)"', attrs)
        if not c or c[1] not in ctx:
            continue
        nm = re.search(r'name="([^"]+)"', attrs)[1].split(":")[-1]
        sc = re.search(r'scale="(-?\d+)"', attrs)
        raw = re.sub(r"<[^>]+>", "", val).replace(",", "").strip()
        if re.fullmatch(r"\d*\.?\d+", raw):
            rows.setdefault(c[1], {"name": ctx[c[1]][0], "date": ctx[c[1]][1]})[nm] = float(raw) * 10 ** int(sc[1] if sc else 0)
    out = []
    for cid, r in rows.items():
        q, cs, v = r.get("InvestmentOwnedBalancePrincipalAmount") or r.get("InvestmentOwnedBalanceShares"), r.get("InvestmentOwnedAtCost"), r.get("InvestmentOwnedAtFairValue")
        if q or cs or v:
            out.append(dict(method="iXBRL tag", name=r["name"][:120], qty=q, cost=cs, value=v, date=r["date"], as_of=iso_long(r["date"]), header="iXBRL concepts", unit="scale attribute of the tag", unit_factor=1, raw=json.dumps({k: v_ for k, v_ in r.items() if k not in ("name", "date")})[:300], table=cid, row="", problem=""))
    return out


def iso_long(d: str) -> str:
    """'2025-03-31' -> 'March 31, 2025', the form of every other as_of."""
    i = re.match(r"(\d{4})-(\d{2})-(\d{2})", d or "")
    return norm_date(f"{MONTHS[int(i[2]) - 1]} {int(i[3])}, {i[1]}") if i and 1 <= int(i[2]) <= 12 else ""


NOTES_TXT = re.compile(r"^notes\s+to\s+(?:(?:the|unaudited|condensed|consolidated|combined)\s+)*financial\s+statements\b" + GAP + r"(?:(?:for\s+the\s+[\w\s-]{0,20}?(?:year|period|months)\s+ended|as\s+of|as\s+at)\s+)?(" + DATE + r")", re.I)


def notes_dates(t: str, filed: str = "") -> list:
    """(position, date, end) of every dated notes heading ('Notes to Financial Statements December 31, 2025', printed on each notes page): a table in
    the notes (restricted securities, fair value levels) is dated by its own page heading, never by the schedule before it. Ends at the next heading."""
    import datetime as dt
    ms = [m for m in END_RAW.finditer(t) if m[0][0].isupper() and AT_START.search(t[max(0, m.start() - 80):m.start()])]
    out = []
    for i, m in enumerate(ms):
        s = text(t[m.start():m.start() + 3000])[:320]
        h = NOTES_TXT.match(s)
        d = norm_date(h[1]) if h and not NOT_HEAD.match(s[h.end():]) else ""
        if d and (not filed or dt.datetime.strptime(d, "%B %d, %Y").date().isoformat() <= filed):
            out.append((m.start(), d, ms[i + 1].start() if i + 1 < len(ms) else len(t)))
    return out


def head_of(heads: list, pos: int):
    """The schedule heading (position, date, end) the position sits under, or None."""
    import bisect
    for h in reversed(heads[:bisect.bisect_right(heads, (pos, "~"))]):
        if pos <= h[2]:
            return h
    return None


def as_of(heads: list, pos: int, period: str) -> str:
    """The date of the nearest schedule heading above the row; else the filing's period of report; else empty, never a guess."""
    h = head_of(heads, pos)
    return h[1] if h else period


CAP_W = r"(?:(?:[A-Z]\.)+|[A-Z](?:[\w&'’/-]|\.(?=\w))*)"      # a capitalised word; a full stop only inside it ('U.S.', 'L.P'), never ending a sentence
FUND_W = r"(?:" + CAP_W + r"|of|and|the|for|in|&|[-–—])"     # a word of a fund name: capitalised, or a joining word
FUND_HEAD = r"(?<![\w&'’./-])(" + CAP_W + r"(?:\s+" + FUND_W + r"){0,12}?(?:(?:\s+|(?<=[a-z]))(?:Funds?|fund|Portfolio|Trust|Series)"
FUND_TAIL = r")(?:,?\s+(?:Inc|L\.?P|LLC|Ltd)\.?)?)$"
FUND_AT_END = re.compile(FUND_HEAD + r"|\s+(?:L\.?P\.?|LLC)" + FUND_TAIL)      # next to a schedule heading a partnership can be the fund ('Megacorn LP')
FUND_ONLY = re.compile(FUND_HEAD + FUND_TAIL)      # an element alone: most portfolio companies are '... LLC' or '... L.P.', so only a fund word counts
GROUP_AT_END = re.compile(r"(?<![\w&'’./-])(" + CAP_W + r"(?:\s+" + FUND_W + r"){0,8}?\s+(?:Series|Trust|Funds))\s+(?:(?i:page)\s+)?\d{1,4}$")
PAGE_MARK = re.compile(r"(?:\s*(?:[-–—|]\s*)?(?:page\s+)?(?:[A-Z]-\s?)?\d{1,4}|\s*\((?:continued|formerly[^()]{0,120})\))+\s*$", re.I)


def fund_name(s: str) -> str:
    """The fund named at the end of a text: a page header or the line above a schedule heading ('American Funds Insurance Series 49 Global Small
    Capitalization Fund', 'SMALLCAP World Fund — Page 104', 'Megacorn Fund LP'). A trust or series printed before the page number joins the name."""
    s = PAGE_MARK.sub("", re.sub(r"\s+", " ", re.sub(r"\btable\s+of\s+contents\b", " ", s, flags=re.I)).strip())
    m = FUND_AT_END.search(s)
    if not m:
        return ""
    name, g = m[1], GROUP_AT_END.search(s[:m.start()].strip())
    return f"{g[1]} {name}" if g and not name.startswith(g[1]) else name


def fund_of(t: str, pos: int, lo: int = 0) -> str:
    """The fund a schedule heading names: between the heading and its date ('Schedule of Investments <fund> <date>'), else on the line above it,
    else the nearest element above it that is a fund name alone (the cover of the fund's report), back to lo (the end of the statements before)."""
    h = HEAD_TXT.match(text(t[pos + (t[pos] == ">"):pos + 3000])[:320])
    if h and h.groupdict().get("fund"):
        return h["fund"].strip(" ,-–—")
    f = fund_name(text(t[max(0, pos - 2500):pos])[-300:])
    if f:
        return f
    lo = max(lo, pos - 40000)
    for m in reversed(list(re.finditer(r">([^<>]{3,200})<", t[lo:pos]))):
        b = t.rfind("<table", lo, lo + m.start())
        if b > t.rfind("</table>", lo, lo + m.start()) and re.search(r"\d,\d{3}|\d\.\d|%", text(t[b:t.find("</table>", lo + m.start())])[:20000]):
            continue      # a cell of a table of amounts or percents (a security or top issuer named '... Trust'), not a title
        x = re.sub(r"[®™℠]", "", text(m[1])).strip()
        n = FUND_ONLY.search(x)
        if n and n.start() == 0:
            return x
    return ""


def fund_continued(t: str, a: int) -> str:
    """The fund a continuation page names above a table ('<fund> (continued)'), or ''."""
    s = text(t[max(0, a - 4000):a])[-400:]
    for m in reversed(list(re.finditer(r"\(continued\)", s, re.I))):
        f = fund_name(s[:m.start()])
        if f:
            return f
    return ""


def heads_of(ctx: dict) -> list:
    """The schedule and dated notes headings of a filing, the schedules' funds and its period of report, read once per filing. A period on or after the filing date
    (the cover date of a prospectus or an N-2) is not the date of any schedule, so it never dates a row."""
    if "heads" not in ctx:
        t, filed = ctx["t"], ctx["filed"]
        ctx["heads"], ctx["notes"], ctx["period"] = schedule_dates(t, filed), notes_dates(t, filed), period_of(t)
        ends = set(ends_of(t))      # a fund's name is looked for back to the end of the schedule before it, never in an earlier fund's tables
        ctx["funds"] = {p: fund_of(t, p, max((e for p_, d_, e in ctx["heads"] if e < p and e in ends), default=0)) for p, d, e in ctx["heads"]}
        import datetime as dt
        if ctx["period"] and filed and dt.datetime.strptime(ctx["period"], "%B %d, %Y").date().isoformat() >= filed:
            ctx["period"] = ""
    return ctx["heads"]


def schedule_of(ctx: dict, pos: int):
    """(start, date, fund) of the schedule a position sits in: its heading, walked back over the repeated headings of the same schedule
    (a heading printed again on each page, same date and fund); None when no dated heading is above it."""
    heads, funds = heads_of(ctx), ctx["funds"]
    h = head_of(heads, pos)
    if not h:
        return None
    i = heads.index(h)
    while i > 0 and heads[i - 1][1:] == h[1:] and funds.get(heads[i - 1][0]) == funds.get(h[0]):
        i -= 1
    return heads[i][0], h[1], funds.get(h[0], "")


def notes_start(ctx: dict, pos: int):
    """Start of the dated notes a position sits in (the first of the notes headings printed page after page with the same date, with no schedule
    heading between), or None."""
    heads = heads_of(ctx)
    notes = ctx["notes"]
    h = head_of(notes, pos)
    if not h:
        return None
    i = notes.index(h)
    while i > 0 and notes[i - 1][1] == h[1] and not any(notes[i - 1][0] < p < notes[i][0] for p, d, e in heads):
        i -= 1
    return notes[i][0]


def width(trs: list) -> int:
    return max((c[1] + 1 for tr in trs[:60] for c in grid(tr)), default=0)


def prior(t: str, a: int, lo: int, key: str, need_unit: bool = False):
    """(header, width) of the nearest earlier table of the same schedule (after lo, its heading) that prints a column header: a schedule run over
    several pages prints its header, and its unit, only at the start. need_unit: the nearest one whose header prints a money unit."""
    pos = a
    for _ in range(300):
        b = t.rfind("<table", lo, pos)
        if b < 0:
            return None
        pos = b
        e = t.find("</table>", b, a)
        if e < 0:
            continue
        trs = re.findall(r"<tr\b[^>]*>(.*?)</tr>", t[b:min(e, b + 150000)], re.S | re.I)
        h = header_of(trs, key)
        if h and (not need_unit or money_unit(h)[0]):
            return h, width(trs)
    return None


def money_unit(h: list):
    """The money unit a header prints: on a cost, value or commitment label, or on a label of its own ('(000)'); qty labels do not count."""
    for c in h:
        k, u = col_of(c[2]), unit_of(c[2])
        if u[0] and k in (None, "cost", "value", "commitment"):
            return u
    return None, ""


def cell_kind(s: str):
    """'date', 'pct' or 'num' for a figure in a cell; None for anything else."""
    if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}(?:\s*-\s*\d{1,2}/\d{1,2}/\d{2,4})?", s):
        return "date"
    if re.search(r"\d\s*%", s):
        return "pct"
    return "num" if NUMBER.match(s) else None


def label_date(label: str) -> str:
    """The date a column label prints ('Fair Value December 31, 2025', 'Value at 12/31/2024'), when the label is only a value and a date."""
    d = re.search(DATE, label) or NUM_DATE.search(label)
    rest = re.sub(DATE + r"|" + NUM_DATE.pattern + r"|\(?\$?\s*000[’'`]?s?\)?|\(\$\)|\b(?:as\s+of|as\s+at|at)\b|[(),$]", " ", label, flags=re.I).split()
    if not d or len(rest) > 2:
        return ""
    return norm_date(d[0]) if d.re.pattern == DATE else num_date(d)


def place(g: list, cols: list, key: str) -> list:
    """(cell, column) for each number of row g. By printed position when every number sits under a column of its own kind; else in order when the
    row has as many numbers as the header has number columns (some filings print the header a column off its numbers), unless a number sits under a
    printed label of no known kind ('Reductions (000)': the row is not of this header); else the nearest column. A percent never goes under a money or share column."""
    cells = [c for c in g if not re.search(key, c[2], re.I) and cell_kind(c[2]) in ("num", "pct")]
    over = lambda c, k: min(c[1], k[1]) - max(c[0], k[0])      # noqa: E731
    fits = lambda c, k: k is not None and k[2] not in (None, "date") and (cell_kind(c[2]) != "pct" or k[2] == "pct")      # noqa: E731
    at = [max((k for k in cols if over(c, k) >= 0), key=lambda k: over(c, k), default=None) for c in cells]
    if all(fits(c, k) for c, k in zip(cells, at)) and len(set(at)) == len(at):
        return list(zip(cells, at))
    ks = [k for k in cols if k[2] not in (None, "date")]
    worded = lambda k: k[2] is None and re.search(r"[A-Za-z]{3}", re.sub(r"thousands?|millions?", " ", k[4], flags=re.I))      # noqa: E731
    if len(ks) == len(cells) and all(fits(c, k) for c, k in zip(cells, ks)) and not any(over(c, k) >= 0 and worded(k) for c in cells for k in cols):
        return list(zip(cells, ks))
    out = []
    for c in cells:
        k = min(cols, key=lambda k: 0 if k[0] <= c[0] <= k[1] or c[0] <= k[1] <= c[1] else min(abs(c[0] - k[0]), abs(c[1] - k[1])))
        out.append((c, k if fits(c, k) else None))
    return out


def from_table(t: str, m, key: str, ctx: dict):
    a = t.rfind("<table", 0, m.start())
    z = t.find("</table>", m.end())
    if a < 0 or z < 0 or t.rfind("</table>", a, m.start()) >= 0 or z - a > 2000000:
        return None
    s = t[a:z]
    trs = re.findall(r"<tr\b[^>]*>(.*?)</tr>", s, re.S | re.I)
    row = next((i for i, tr in enumerate(trs) if key_in(tr, key) and t[a:m.start()].count("<tr") - 1 == i or False), None)
    if row is None:
        row = sum(1 for _ in re.finditer(r"<tr\b", t[a:m.start()], re.I)) - 1
    if row < 0 or row >= len(trs):
        return None
    g = grid(trs[row])
    if not any(re.search(key, c[2], re.I) for c in g):
        return None
    if len(g) == 1 and row + 1 < len(trs):     # the issuer on its own row, its instrument and numbers on the next
        nx = grid(trs[row + 1])
        if any(NUMBER.match(c[2]) for c in nx):
            g = g + nx
    if len(g) == 2 and re.fullmatch(r"\d{1,2}\.\d{1,2}%?", g[1][2]):
        return dict(problem="percent summary, not a position", name=g[0][2][:120], raw=" | ".join(c[2] for c in g)[:300], table=a, row=row, header="", unit="", unit_factor=None, method="table", qty=None, cost=None, value=None, date="")
    h, src = header_of(trs[:row], key), "same table"
    sch = schedule_of(ctx, a)
    nums = [c for c in g if not re.search(key, c[2], re.I) and cell_kind(c[2]) in ("num", "pct")]
    fit = lambda lay: lay and (lay[1] == width(trs) or sum(col_of(c[2]) not in (None, "date") for c in lay[0]) == len(nums))      # noqa: E731
    if h:
        ctx["last"] = (h, trs)
    elif sch:      # a continuation page of the schedule: the layout of the nearest earlier table of the same schedule, when it fits this row
        lay = prior(t, a, sch[0], key)
        if fit(lay):
            h, src = lay[0], "earlier table of this schedule"
    elif ctx.get("last") and fit((ctx["last"][0], width(ctx["last"][1]))):      # no dated schedule: the last header read in the file, when it fits this row
        h, src = ctx["last"][0], "earlier table"
    if not h and not any(NUMBER.match(c[2]) and float(NUMBER.match(c[2])[1].replace(",", "")) >= 100 for c in g):
        if shares_named(g, key):     # a share count in words and no amount (a pledge or ownership schedule): a position with shares only
            return dict(method="table", name=next((c[2] for c in g if re.search(key, c[2], re.I)), "")[:140], qty=shares_named(g, key), qty_factor=1, cost=None, value=None,
                        commitment=None, date="", table=a, row=row, header="no column header; share count printed beside the company", unit="shares only, no amount printed",
                        unit_factor=1, raw=" | ".join(c[2] for c in g)[:300], problem="", fund=(sch[2] if sch else "") or fund_continued(t, a))
        return dict(problem="not a position: no amount in the row", name=g[0][2][:120], raw=" | ".join(c[2] for c in g)[:300], table=a, row=row, header="", unit="", unit_factor=None, method="table", qty=None, cost=None, value=None, date="")
    if not h:
        return dict(problem="no column header found", name=g[0][2][:120], raw=" | ".join(c[2] for c in g)[:300], table=a, row=row, header="", unit="", unit_factor=None, method="table", qty=None, cost=None, value=None, date="")
    cols = [(c[0], c[1], col_of(c[2]), unit_of(c[2]), c[2], label_date(c[2])) for c in h]
    base = dict(method="table", name="", qty=None, cost=None, value=None, commitment=None, date="", table=a, row=row, header=" | ".join(c[2] for c in h)[:200] + f" [{src}]", raw=" | ".join(c[2] for c in g)[:300], problem="",
                fund=(sch[2] if sch else "") or fund_continued(t, a))
    base["name"] = next((c[2] for c in g if re.search(key, c[2], re.I)), "")[:140]
    dates = [c[2] for c in g if not re.search(key, c[2], re.I) and cell_kind(c[2]) == "date"]
    if dates:
        base["date"] = dates[-1]
    placed = place(g, cols, key)
    periods = sorted({k[5] for c, k in placed if k and k[2] == "value" and k[5]})
    out = []
    for n, d in enumerate(periods if len(periods) > 1 else [""]):      # one row per dated value column ('Fair Value December 31, 2025 | ... 2024')
        r, units = dict(base), {}
        for c, k in placed:
            if k is None or k[2] not in ("qty", "cost", "value", "commitment") or (d and k[5] and k[5] != d) or r[k[2]] is not None:
                continue
            r[k[2]] = float(NUMBER.match(c[2])[1].replace(",", ""))
            units[k[2]] = k[3]
        if d:
            r["as_of"], r["row"] = d, f"{row}-{n + 1}"
        out.append(unit_row(r, units, cols, t, a, trs, g, key, sch, sch[0] if sch else notes_start(ctx, a)))
    return out if len(out) > 1 else out[0]


def unit_row(r: dict, units: dict, cols: list, t: str, a: int, trs: list, g: list, key: str, sch, sec=None) -> dict:
    """Set the unit of a table row: its column label, a unit label of the same header, a unit line above the table, the header of an earlier
    table of the same schedule with as many number columns, a '$' in the header, or in the body when the schedule (or the dated notes) prints no thousands or millions
    anywhere above; else none printed."""
    g_cost = re.search(r"cost\s*(?:[:—–-]\s*)?\$\s*([\d,]+(?:\.\d+)?)", r["name"], re.I)
    if g_cost and r["cost"] is None:
        r["cost"] = float(g_cost[1].replace(",", ""))
        units["cost"] = units.get("value") or (None, "")
    pu = page_unit(t, a)
    f = [u for k, u in units.items() if k != "qty" and u and u[0]]
    loose = money_unit([(c[0], c[1], c[4]) for c in cols if not c[2]])
    up = None
    if not f and not loose[0] and not pu[0] and sch:
        n = sum(c[2] not in (None, "date") for c in cols)
        lay = prior(t, a, sch[0], key, need_unit=True)
        up = money_unit(lay[0]) if lay and sum(col_of(c[2]) not in (None, "date") for c in lay[0]) == n else None
    if f:
        r["unit_factor"], r["unit"] = f[0]
    elif loose[0]:
        r["unit_factor"], r["unit"] = loose
    elif pu[0]:
        r["unit_factor"], r["unit"] = pu
    elif up and up[0]:
        r["unit_factor"], r["unit"] = up[0], f"{up[1]} (header of an earlier table of this schedule)"
    elif "$" in r["header"] and not re.search(r"\b000|thousand|million", r["header"], re.I):
        r["unit_factor"], r["unit"] = 1, "dollars, $ in the column header"
    elif sec is not None and any(c[2].startswith("$") for tr in trs[:60] for c in grid(tr)) and not re.search(r"\b000|thousand|million", r["header"], re.I) \
            and not re.search(r"\(\s*\$?\s*000[’'`]?s?\s*\)|\$\s?000\b|thousands|millions", t[sec:a], re.I):     # nowhere in the schedule or dated notes; with neither, no bound: stays unprinted
        r["unit_factor"], r["unit"] = 1, "dollars, $ printed in the table, no thousands or millions"
    else:
        r["unit_factor"], r["unit"] = 1, "no unit printed; dollars"
    if r["qty"] is None and shares_named(g, key):      # no share column: the count printed in the security name
        r["qty"], units["qty"] = shares_named(g, key), (1, "")
    q = units.get("qty")
    label = next((k[4] for k in cols if k[2] == "qty"), "")
    money_qty = bool(re.search(r"principal|\bpar\b|amount|face", label, re.I)) and not re.search(r"shares", label, re.I)   # par is in the table's money unit
    r["qty_factor"] = q[0] if q and q[0] else (r["unit_factor"] if money_qty else 1)
    if r["qty"] is None and r["cost"] is None and r["value"] is None and r["commitment"] is None:
        r["problem"] = "no number under a known column" if any(NUMBER.match(c) and float(NUMBER.match(c)[1].replace(",", "")) >= 100 for c in (x[2] for x in g)) else "not a position: no amount in the row"
    return r


def shares_named(g: list, key: str):
    """A share count printed in words beside the company, not under a column: in the security name ('Acme Corp. (225,367 common shares)',
    '828,980 shares of Series A Preferred Stock of Acme, Inc.') or in a cell of its own in parentheses ('(225,367 common shares)')."""
    for c in g:
        if len(c[2]) <= 160 and (re.search(key, c[2], re.I) or re.fullmatch(r"\(.*\)", c[2])):
            m = SHARES_TXT.search(c[2])
            if m:
                return float(m[1].replace(",", ""))
    return None


def key_in(tr, key):
    return bool(re.search(key, tr, re.I))


def from_text(t: str, m, key: str):
    """Layouts without table rows: name first, then the footnotes, then shares and value."""
    x = text(t[m.start():m.start() + 1400])
    r = re.match(r"(.{0,170}?(?:\)|\.{2,}|\bDate:[^,]{0,20}, Cost \$[\d,]+[^0-9]{0,4}))\s*(?:\([\da-z]+\)\s*)*[.\s]*(\d[\d,]*)\s+(\d[\d,]*)\b", x)
    if not r or not re.search(r"series|inc|preferred|common", r[1], re.I):
        return None
    z = text(t[max(0, m.start() - 1500):m.start()])
    if not re.search(r"\.{2,}|Cost \$|\(\d\)|\([a-z]\)", r[0]):
        return None
    f, u = page_unit(t, m.start())
    c = re.search(r"cost[:\s]*\$\s*([\d,]+(?:\.\d+)?)", r[0], re.I)
    return dict(method="text layout", name=r[1][:140], qty=float(r[2].replace(",", "")), cost=float(c[1].replace(",", "")) if c else None, value=float(r[3].replace(",", "")), date="", table="text", row=m.start(), header="shares then value, as laid out", unit=u or "no unit printed; dollars", unit_factor=f or 1, raw=x[:300], problem="", qty_factor=1)


def from_pdf(p: Path, key: str, filed: str = "") -> list:
    """PDF lines naming the company. Numbers are read right to left against the nearest printed header line above; a line that does not fit goes to review."""
    import pypdfium2
    doc = pypdfium2.PdfDocument(str(p))
    out = []
    labels = [("date", r"date"), ("qty", r"shares|principal|par\b|units"), ("cost", r"cost"), ("value", r"value")]
    last, lastf = "", ""      # a schedule runs over pages: a page with no heading keeps the date and fund of the last heading
    for i in range(len(doc)):
        pg = doc[i]
        tp = pg.get_textpage()
        txt = tp.get_text_range()
        tp.close()
        pg.close()
        lines = [re.sub(r"\s+", " ", x).strip() for x in txt.splitlines()]
        at = [0]      # each line as its own element, so the heading rules for html apply to the page
        for x in lines:
            at.append(at[-1] + len(x) + 7)
        pt = "".join(f"<p>{x}</p>" for x in lines)
        hd, prev, prevf = schedule_dates(pt, filed), last, lastf
        ends = any(m[0][0].isupper() and AT_START.search(pt[max(0, m.start() - 80):m.start()]) for m in END_RAW.finditer(pt))
        last = next((d for p_, d, e in reversed(hd) if e == len(pt)), "" if ends else last)     # a statement or notes page ends the schedule
        lastf = next((fund_of(pt, p_) for p_, d, e in reversed(hd) if e == len(pt)), "" if ends else lastf)
        if not re.search(key, txt, re.I):
            continue
        u = PAGE_UNIT.search(re.sub(r"\s+", " ", txt))
        unit = (1e6 if "million" in u[1].lower() else 1e3) if u else None
        for j, ln in enumerate(lines):
            if not re.search(key, ln, re.I):
                continue
            nums = [float(x.replace(",", "")) for x in re.findall(r"(?<![\d/.])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\d/])", ln)]
            if not nums:
                continue
            head = next((h for h in reversed(lines[max(0, j - 60):j]) if re.search(r"\b(value|cost)\b", h, re.I) and not re.search(r"\d,\d{3}", h) and len(h) < 200), "")
            order = sorted((re.search(rx, head, re.I).start(), n) for n, rx in labels[1:] if re.search(rx, head, re.I))
            cols = [n for _, n in order]
            r = dict(method="pdf line", name=ln[:140], qty=None, cost=None, value=None, date="", table=f"page {i + 1}", row=j, header=head[:200], unit="" , unit_factor=unit or 1, raw=ln[:300], problem="", qty_factor=1, as_of=as_of(hd, at[j], prev))
            r["unit"] = u[0][:60] if u else "no unit printed; dollars"
            hh = head_of(hd, at[j])
            r["fund"] = fund_of(pt, hh[0]) if hh else prevf
            if cols and len(nums) >= len(cols):
                for n, v in zip(cols, nums[-len(cols):]):
                    r[n] = v
            else:
                r["problem"] = "pdf line does not fit the printed header"
            out.append(r)
    doc.close()
    return out


def scan(job: tuple) -> dict:
    path, key, cache, filed = job
    c = Path(cache) / (hashlib.sha1((path + CACHE_V + key).encode()).hexdigest() + ".json")
    if c.exists():
        return json.loads(c.read_text(encoding="utf-8"))
    res = {"path": path, "rows": [], "other": 0, "error": ""}
    try:
        p = Path(path)
        if p.suffix.lower() == ".pdf":
            res["rows"] += from_pdf(p, key, filed)
        else:
            t = p.read_text(encoding="utf-8", errors="ignore")
            t = re.sub(r"</?(?:table|tr|td|th)\b", lambda x: x[0].lower(), t, flags=re.I)     # older filings use capital tags
            tagged = from_ixbrl(t, key)
            res["rows"] += tagged
            seen, ctx = set(), {"t": t, "filed": filed}
            for m in re.finditer(key, t, re.I):
                r = from_table(t, m, key, ctx)
                if r is None:
                    r = from_text(t, m, key)
                if r is None:
                    res["other"] += 1
                    continue
                for x in r if isinstance(r, list) else [r]:
                    k = (x.get("table"), x.get("row"))
                    if k in seen:
                        continue
                    seen.add(k)
                    heads = heads_of(ctx)
                    x["as_of"] = x.get("as_of") or as_of(heads, m.start(), "") or as_of(ctx["notes"], m.start(), ctx["period"])
                    if "fund" not in x:
                        h = head_of(heads, m.start())
                        x["fund"] = ctx["funds"].get(h[0], "") if h else ""
                    res["rows"].append(x)
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"[:160]
    c.write_text(json.dumps(res), encoding="utf-8")
    return res


def cell_key(names: list) -> str:
    """Names that count only when they fill a whole table cell (a schedule printing 'Outreach' for 'Outreach Corp'): as their own text node in
    the html, or as the whole text of a cell, footnote marks allowed; the case printed. A name in prose never matches this way."""
    ns = sorted({n.strip() for n in names if n and n.strip()}, key=len, reverse=True)
    if not ns:
        return ""
    sp = r"(?:\s|&nbsp;|&#160;|&#xa0;)*"
    return r"|(?:(?<=>)|^)" + sp + r"(?-i:" + "|".join(re.escape(n).replace(r"\ ", r"\s+") for n in ns) + r")" + sp + r"(?:(?:\(\s*[a-z0-9]{1,3}\s*\)|[*†‡])" + sp + r")*(?=<|$)"


def limit() -> None:
    """Cap a worker's memory where the OS allows it. macOS refuses RLIMIT_AS (ValueError); a worker that dies here hangs the pool, so go on without the cap."""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (1400 * 1024 ** 2, 1400 * 1024 ** 2))
    except (ImportError, ValueError, OSError):
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("company")
    ap.add_argument("--folders", nargs="+", required=True)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--dumps", default=str(HERE.parents[2] / "dumps"))
    ap.add_argument("--only", default="")
    ap.add_argument("--cell-names", nargs="*", default=None, help="short names that count only as a whole table cell ('Outreach' in a schedule row); "
                    "default: model_config company.cell_names, else its aliases and employee_company")
    a = ap.parse_args()
    cfg = json.loads((HERE / "model_config" / f"{a.company}.json").read_text(encoding="utf-8"))
    co = cfg["company"]
    names = a.cell_names if a.cell_names is not None else co.get("cell_names", co.get("aliases", []) + [co.get("employee_company") or ""])
    key, ev = co["name_regex"] + cell_key(names), HERE / "out" / a.company / "evidence"
    cache = ev / "table_cache"
    cache.mkdir(parents=True, exist_ok=True)
    files = {}
    for f in a.folders:
        for r in csv.DictReader(open(Path(a.dumps) / f"{f}_manifest.csv", encoding="utf-8", newline="")):
            p = Path(a.dumps) / f / r["file"]
            if p.exists() and p.suffix.lower() in (".htm", ".html", ".pdf") and (not a.only or re.search(a.only, r["form"])):
                files[str(p)] = r
    jobs = sorted(((p, key, str(cache), files[p].get("file_date", "")[:10]) for p in files), key=lambda j: Path(j[0]).stat().st_size)
    head = ["accession", "form", "file_date", "filer", "fund", "as_of", "method", "name", "qty", "qty_factor", "cost", "value", "commitment", "unit_factor", "date", "header", "unit", "raw", "table", "row", "problem", "url"]
    n = collections.Counter()
    with mp.Pool(a.workers, initializer=limit, maxtasksperchild=25) as pool, open(ev / "parsed_rows.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, head, extrasaction="ignore")
        w.writeheader()
        for i, s in enumerate(pool.imap_unordered(scan, jobs, chunksize=1)):
            m = files[s["path"]]
            n["other_mentions"] += s["other"]
            n["errors"] += bool(s["error"])
            for r in s["rows"]:
                n[r["method"] + (" problem" if r.get("problem") else "")] += 1
                w.writerow({**r, "accession": m["accession"], "form": m["form"], "file_date": m["file_date"], "filer": m["filer"], "url": m["sec_url"]})
            if i % 200 == 0:
                print("scanned", i, "of", len(jobs), flush=True)
    print(json.dumps(dict(n)))


if __name__ == "__main__":
    main()
