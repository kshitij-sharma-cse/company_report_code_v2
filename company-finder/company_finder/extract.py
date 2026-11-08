"""Text extraction for common document types.

Each extractor yields :class:`TextUnit` objects: a piece of text plus where it
came from (PDF page, DOCX paragraph/table cell, sheet/cell, slide, ...).
Mention offsets are relative to ``TextUnit.text`` (or to the cell value for
tabular units, see ``cells``).
"""
from __future__ import annotations

import csv
import io
import logging
import math
import re
import warnings
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .config import DEFAULT_ENTITY_WORDS, DEFAULT_LEGAL_SUFFIXES
from .sec import (SectionTracker, classify_sgml_document, exhibit_label, form_d_fields,
                  is_form_d_xml, looks_like_sgml_submission, parse_form_d_html,
                  parse_form_d_xml, parse_sgml_submission, strip_sgml_wrappers)

log = logging.getLogger(__name__)

PDF_EXT = {".pdf"}
DOCX_EXT = {".docx", ".docm"}
TEXT_EXT = {".txt", ".text", ".md", ".markdown", ".rst", ".log", ".json"}
HTML_EXT = {".html", ".htm", ".xhtml"}
XML_EXT = {".xml"}
XLSX_EXT = {".xlsx", ".xlsm"}
CSV_EXT = {".csv", ".tsv"}
PPTX_EXT = {".pptx"}
LEGACY_EXT = {".doc": "docx", ".xls": "xlsx", ".ppt": "pptx", ".rtf": "docx or pdf"}
SUPPORTED_EXT = PDF_EXT | DOCX_EXT | TEXT_EXT | HTML_EXT | XML_EXT | XLSX_EXT | CSV_EXT | PPTX_EXT

CELL_SEP = " | "


@dataclass
class TextUnit:
    text: str
    location: dict                      # e.g. {"page": 3} / {"sheet": "S1", "row": 4}
    # tabular rows: [(start, end, extra_location)] spans of each cell in text
    cells: list[tuple[int, int, dict]] | None = None
    line_numbers: bool = False          # compute "line" from offsets (plain text)
    section: str | None = None          # filing section at the start of the unit
    sections: list[tuple[int, str]] | None = None  # (offset, label) changes inside
    cik: str | None = None              # structured CIK for this unit (Form D issuer)


@dataclass
class ExtractOptions:
    ocr: bool = False
    ocr_lang: str = "eng"
    ocr_min_chars: int = 25
    ocr_dpi: int = 300
    strip_headers: bool = True
    dehyphenate: bool = True


@dataclass
class ExtractResult:
    path: str
    doc_type: str
    units: list[TextUnit] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: dict = field(default_factory=dict)


# --------------------------------------------------------------- utilities
@lru_cache(maxsize=1)
def _common_words() -> frozenset:
    p = Path(__file__).parent / "data" / "common_words.txt"
    try:
        return frozenset(l.strip() for l in p.read_text(encoding="utf-8").splitlines()
                         if l.strip() and not l.startswith("#"))
    except OSError:
        return frozenset()


_HYPH_RE = re.compile(r"(\w+)-$")


def join_lines(lines: list[str], dehyphenate: bool = True) -> str:
    """Join visually wrapped lines of one paragraph into a single line.

    Line-end hyphens are removed when the next line starts lower-case and the
    joined word looks like a word ("manufac-" + "turing"); kept for genuine
    compounds whose parts are both words ("long-" + "term"); kept without a
    space before a capital ("Coca-" + "Cola")."""
    out = ""
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if not out:
            out = line
            continue
        m = _HYPH_RE.search(out) if dehyphenate else None
        if m and line[:1].isalpha():
            if line[:1].islower():
                left = m.group(1)
                right = re.match(r"\w+", line).group()
                cw = _common_words()
                joined = (left + right).lower()
                if joined in cw or not (left.lower() in cw and right.lower() in cw):
                    out = out[:-1] + line
                else:
                    out = out + line
            else:
                out = out + line
        else:
            out = out + " " + line
    return out


def _paragraphs(text: str, dehyphenate: bool) -> str:
    """Plain/OCR text: blank lines separate paragraphs; wrap lines joined."""
    paras = re.split(r"\n\s*\n", text)
    return "\n\n".join(p for p in (join_lines(pp.splitlines(), dehyphenate) for pp in paras) if p)


def _sectioned_unit(text: str, location: dict, tracker: SectionTracker, **kw) -> TextUnit:
    """TextUnit whose section markers come from feeding each line to tracker."""
    start_label = tracker.label
    markers = []
    pos = 0
    for line in text.split("\n"):
        if tracker.feed(line):
            markers.append((pos, tracker.label))
        pos += len(line) + 1
    return TextUnit(text, location, section=start_label, sections=markers or None, **kw)


def _read_text_file(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


# --------------------------------------------------------------------- PDF
_tesseract_state: dict = {}


def tesseract_available() -> tuple[bool, str]:
    if "ok" not in _tesseract_state:
        try:
            import pytesseract
            v = pytesseract.get_tesseract_version()
            _tesseract_state.update(ok=True, msg=f"tesseract {v}")
        except ImportError:
            _tesseract_state.update(ok=False, msg="pytesseract not installed (pip install pytesseract)")
        except Exception:
            _tesseract_state.update(ok=False, msg=(
                "tesseract binary not found (install it, e.g. `apt install tesseract-ocr` "
                "or `brew install tesseract`)"))
    return _tesseract_state["ok"], _tesseract_state["msg"]


def _sig(text: str) -> str:
    return re.sub(r"\d+", "#", " ".join(text.split()).lower())


def extract_pdf(path: Path, opts: ExtractOptions) -> ExtractResult:
    try:
        import pymupdf as fitz
    except ImportError:  # older PyMuPDF
        import fitz

    res = ExtractResult(str(path), "pdf")
    with fitz.open(path) as doc:
        if doc.needs_pass:
            res.warnings.append("encrypted PDF: cannot extract text")
            return res
        n = doc.page_count
        pages = []  # list of (height, [(y0, y1, text)])
        for page in doc:
            h = page.rect.height or 1
            blocks = []
            for b in page.get_text("blocks", sort=True):
                if b[6] != 0:  # image block
                    continue
                txt = join_lines(b[4].splitlines(), opts.dehyphenate)
                if txt:
                    blocks.append((b[1], b[3], txt))
            pages.append((h, blocks))

        # repeated headers/footers: same signature in top/bottom 12% of many pages
        drop = set()
        if opts.strip_headers and n >= 3:
            cnt = Counter()
            for h, blocks in pages:
                seen = set()
                for y0, y1, txt in blocks:
                    if y1 < 0.12 * h or y0 > 0.88 * h:
                        seen.add(_sig(txt))
                cnt.update(seen)
            need = max(3, math.ceil(0.6 * n))
            drop = {s for s, c in cnt.items() if c >= need}
            if drop:
                res.info["dropped_header_footer_patterns"] = sorted(drop)

        ocr_pages, scanned = [], []
        tracker = SectionTracker()
        for pno, (h, blocks) in enumerate(pages, 1):
            kept = [txt for y0, y1, txt in blocks
                    if not (_sig(txt) in drop and (y1 < 0.12 * h or y0 > 0.88 * h))]
            text = "\n\n".join(kept)
            if len(text.strip()) < opts.ocr_min_chars:
                if opts.ocr:
                    ok, msg = tesseract_available()
                    if ok:
                        import pytesseract
                        from PIL import Image
                        pix = doc[pno - 1].get_pixmap(dpi=opts.ocr_dpi)
                        img = Image.frombytes("RGB" if pix.n < 4 else "RGBA",
                                              (pix.width, pix.height), pix.samples)
                        text = _paragraphs(pytesseract.image_to_string(img, lang=opts.ocr_lang),
                                           opts.dehyphenate)
                        ocr_pages.append(pno)
                    else:
                        if not any("OCR unavailable" in w for w in res.warnings):
                            res.warnings.append(f"OCR unavailable: {msg}")
                        scanned.append(pno)
                else:
                    scanned.append(pno)
            if text.strip():
                res.units.append(_sectioned_unit(text, {"page": pno}, tracker))
        res.info["pages"] = n
        if ocr_pages:
            res.info["ocr_pages"] = ocr_pages
        if scanned:
            res.warnings.append(
                f"{len(scanned)} page(s) with little/no text layer (scanned?): "
                f"{scanned[:20]}{'...' if len(scanned) > 20 else ''}"
                + ("" if opts.ocr else " - rerun with --ocr"))
    return res


# -------------------------------------------------------------------- DOCX
def _row_unit(values: list[tuple[str, dict]], location: dict) -> TextUnit | None:
    parts, cells, pos = [], [], 0
    for val, loc in values:
        val = " ".join(str(val).split()) if val is not None else ""
        if not val:
            continue
        if parts:
            pos += len(CELL_SEP)
        cells.append((pos, pos + len(val), loc))
        parts.append(val)
        pos += len(val)
    if not parts:
        return None
    return TextUnit(CELL_SEP.join(parts), dict(location), cells=cells)


def extract_docx(path: Path, opts: ExtractOptions) -> ExtractResult:
    import docx
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    res = ExtractResult(str(path), "docx")
    d = docx.Document(str(path))
    counters = {"paragraph": 0, "table": 0}
    tracker = SectionTracker()

    def do_table(tbl: Table, prefix: str):
        counters["table"] += 1
        tno = f"{prefix}{counters['table']}"
        for r, row in enumerate(tbl.rows, 1):
            seen, vals = set(), []
            for c, cell in enumerate(row.cells, 1):
                if id(cell._tc) in seen:  # merged cells repeat
                    continue
                seen.add(id(cell._tc))
                vals.append(("\n".join(p.text for p in cell.paragraphs), {"col": c}))
                for inner in cell.tables:
                    do_table(inner, f"{tno}.")
            u = _row_unit(vals, {"table": tno, "row": r})
            if u:
                tracker.feed(u.text)
                u.section = tracker.label
                res.units.append(u)

    for child in d.element.body.iterchildren():
        if child.tag == qn("w:p"):
            counters["paragraph"] += 1
            txt = Paragraph(child, d).text
            if txt.strip():
                tracker.feed(txt)
                res.units.append(TextUnit(txt, {"paragraph": counters["paragraph"]},
                                          section=tracker.label))
        elif child.tag == qn("w:tbl"):
            do_table(Table(child, d), "")

    seen_hf = set()
    for s_idx, section in enumerate(d.sections, 1):
        for part_name in ("header", "footer"):
            try:
                part = getattr(section, part_name)
            except Exception:
                continue
            txt = "\n".join(p.text for p in part.paragraphs).strip()
            if txt and txt not in seen_hf:
                seen_hf.add(txt)
                res.units.append(TextUnit(txt, {"part": part_name, "section": s_idx}))
    res.info["paragraphs"] = counters["paragraph"]
    res.info["tables"] = counters["table"]
    return res


# -------------------------------------------------------------- text / html
def extract_text(path: Path, opts: ExtractOptions) -> ExtractResult:
    txt = _read_text_file(path)
    if looks_like_sgml_submission(txt):
        return extract_edgar_submission(path, txt, opts)
    res = ExtractResult(str(path), "text")
    if txt.strip():
        res.units.append(_sectioned_unit(txt, {}, SectionTracker(), line_numbers=True))
    return res


_BLOCK_TAGS = ["p", "div", "section", "article", "header", "footer", "li", "ul", "ol",
               "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table", "blockquote", "pre",
               "dd", "dt", "caption", "title", "main", "aside", "nav", "form"]
_COL_GAP = re.compile(r"[ \t\xa0]{2,}")  # column gap in a <pre> table
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none", re.I)
_CONT_WORDS = ({s.lower() for s in DEFAULT_LEGAL_SUFFIXES} | {s.lower() for s in DEFAULT_ENTITY_WORDS}
               | {"ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "l", "p", "n", "a"})
_TABLE_MARK = "\x00TBL{}\x00"


def _is_continuation(prev: str, nxt: str) -> bool:
    """Does table cell text ``nxt`` (alone on its row) continue ``prev`` from the
    row above?  e.g. "Acme Intermediate Holdings" + "II, LLC", "Acme Buyer," + "Inc."."""
    if not prev or not nxt or len(nxt) > 80:
        return False
    p = prev.rstrip()
    if p[-1:] in (",", "&", "-", "/", "(") or p.split()[-1].lower() in ("and", "of", "the", "for", "&"):
        return True
    if nxt[:1].islower():
        return True
    first = re.sub(r"[^\w/]", "", nxt.split()[0]).lower()
    return first in _CONT_WORDS or nxt.lower().startswith(("(d/b/a", "d/b/a", "(f/k/a", "f/k/a", "(dba"))


def _table_rows(table) -> list[list[tuple[int, str]]]:
    rows = []
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is not table:
            continue
        col, cells = 0, []
        for cell in tr.find_all(["td", "th"], recursive=False):
            txt = " ".join(cell.get_text(" ").split())
            if re.search(r"\w", txt):
                cells.append((col, txt))
            try:
                col += max(1, int(cell.get("colspan", 1)))
            except ValueError:
                col += 1
        if cells:
            rows.append(cells)
    merged: list[list[tuple[int, str]]] = []
    for row in rows:
        if merged and len(row) == 1:
            c, txt = row[0]
            prev = merged[-1]
            for k, (pc, ptxt) in enumerate(prev):
                if pc == c and _is_continuation(ptxt, txt):
                    prev[k] = (pc, ptxt + " " + txt)
                    break
            else:
                merged.append(list(row))
            continue
        merged.append(list(row))
    return merged


def _registrant_from_cover(text: str) -> str | None:
    lines = [l.strip() for l in text.split("\n")]
    for i, l in enumerate(lines):
        if re.search(r"exact name of (?:the )?registrant", l, re.I):
            for back in range(i - 1, max(-1, i - 6), -1):
                cand = lines[back].strip(" |")
                if re.search(r"[A-Za-z]{2}", cand) and not re.search(r"commission file|number", cand, re.I):
                    return " ".join(cand.split())
    return None


def html_to_units(data: bytes | str, res: ExtractResult, base_loc: dict | None = None,
                  tracker: SectionTracker | None = None) -> dict:
    """Extract an (i)XBRL/HTML document into res.units.  Tables become row units
    (one cell = one match scope, wrapped names merged); text between tables
    becomes text units with section markers.  Returns filer facts found."""
    from bs4 import BeautifulSoup, NavigableString

    base_loc = dict(base_loc or {})
    tracker = tracker or SectionTracker()
    with warnings.catch_warnings():  # iXBRL starts with <?xml ...?> but is XHTML
        warnings.simplefilter("ignore")
        soup = BeautifulSoup(data, "lxml")

    facts: dict = {}
    for t in soup.find_all(lambda x: x.name in ("ix:nonnumeric", "ix:nonfraction")):
        nm = (t.get("name") or "").lower()
        val = " ".join(t.get_text(" ").split())
        if nm.endswith(":entityregistrantname") and val and "name" not in facts:
            facts["name"] = val
        elif nm.endswith(":entitycentralindexkey") and val:
            facts["cik"] = val
        elif nm.endswith(":documenttype") and val:
            facts["form_type"] = val
    if soup.find(lambda x: x.name and x.name.startswith("ix:")):
        res.info["inline_xbrl"] = True

    fd = parse_form_d_html(soup)
    if fd:
        _form_d_units(fd, res, base_loc)
        return {"form_d": fd}

    hidden = 0
    hidden_classes = set()
    for st in soup.find_all("style"):
        for mm in re.finditer(r"([^{}]+)\{([^}]*)\}", st.get_text()):
            if _HIDDEN_STYLE.search(mm.group(2)):
                hidden_classes.update(re.findall(r"\.([\w-]+)\s*(?:,|$)", mm.group(1).strip()))
    for t in soup.find_all(lambda x: x.name in ("ix:header", "script", "style", "noscript",
                                                "template", "svg", "head")):
        t.decompose()
    for t in soup.find_all(style=_HIDDEN_STYLE):
        t.decompose()
        hidden += 1
    if hidden_classes:  # simple ".cls { display:none }" rules from <style>
        for t in soup.find_all(class_=lambda c: c and c in hidden_classes):
            if not t.decomposed:
                t.decompose()
                hidden += 1
    if hidden:
        res.info["hidden_elements_skipped"] = res.info.get("hidden_elements_skipped", 0) + hidden
    for br in soup.find_all("br"):
        br.replace_with("\n")

    tables = []
    for t in soup.find_all("table"):
        if t.find_parent("table") is None:
            tables.append(t)
    rows_per_table = []
    for n, t in enumerate(tables):
        rows_per_table.append(_table_rows(t))
        t.replace_with(soup.new_string("\n" + _TABLE_MARK.format(n) + "\n"))
    for pre in soup.find_all("pre"):  # fixed-width schedule: keep column gaps (sentinel survives the collapse)
        for s in pre.find_all(string=True):
            if type(s) is NavigableString and _COL_GAP.search(s):
                s.replace_with(NavigableString(_COL_GAP.sub("\x01", s)))
    for t in soup.find_all(_BLOCK_TAGS):
        t.append("\n")
    txt = soup.get_text()
    txt = re.sub(r"[ \t\r\f\v\xa0]+", " ", txt)
    txt = re.sub(r" *\n *", "\n", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    txt = re.sub(r"(?m)^[ \x01]+|[ \x01]+$", "", txt) if "\x01" in txt else txt
    txt = re.sub(r" ?\x01+ ?", "  ", txt)

    if "name" not in facts:
        cover = _registrant_from_cover(re.sub(r"\x00TBL\d+\x00", "", txt))
        if cover:
            facts.setdefault("name", cover)
            facts["source"] = "cover-page"
    elif facts:
        facts["source"] = "ixbrl-dei"

    block = 0
    for piece in re.split(r"(\x00TBL\d+\x00)", txt):
        m = re.fullmatch(r"\x00TBL(\d+)\x00", piece)
        if m:
            tno = int(m.group(1)) + 1 + res.info.get("_table_offset", 0)
            for r, row in enumerate(rows_per_table[int(m.group(1))], 1):
                u = _row_unit([(v, {"col": c + 1}) for c, v in row],
                              {**base_loc, "table": tno, "row": r})
                if u:
                    tracker.feed(u.text)
                    u.section = tracker.label
                    res.units.append(u)
            continue
        piece = piece.strip()
        if piece:
            block += 1
            res.units.append(_sectioned_unit(piece, {**base_loc, "text_block": block}, tracker))
    res.info["_table_offset"] = res.info.get("_table_offset", 0) + len(tables)
    res.info["tables"] = res.info.get("_table_offset", 0)
    return facts


def _form_d_units(rec: dict, res: ExtractResult, base_loc: dict | None = None) -> None:
    for fld, section, text, cik in form_d_fields(rec):
        res.units.append(TextUnit(text, {**(base_loc or {}), "form_d_field": fld},
                                  section=section, cik=cik))
    res.info["form_d"] = rec
    iss = (rec.get("issuers") or [{}])[0]
    if iss.get("entity_name"):
        res.info["filer"] = {"name": iss["entity_name"], "cik": iss.get("cik"),
                             "form_type": rec.get("submission_type") or "D",
                             "former_names": iss.get("previous_names", []) +
                             iss.get("edgar_previous_names", []),
                             "role": "issuer", "source": f"form-d-{rec.get('source')}"}


def extract_html(path: Path, opts: ExtractOptions) -> ExtractResult:
    res = ExtractResult(str(path), "html")
    facts = html_to_units(path.read_bytes(), res)
    if "form_d" in facts:
        res.doc_type = "form_d_html"
    elif facts.get("name") or facts.get("cik"):
        res.info["filer"] = {**facts, "former_names": []}
    res.info.pop("_table_offset", None)
    return res


def extract_xml(path: Path, opts: ExtractOptions) -> ExtractResult:
    from bs4 import BeautifulSoup

    data = path.read_bytes()
    if is_form_d_xml(data):
        res = ExtractResult(str(path), "form_d")
        rec = parse_form_d_xml(data)
        if rec:
            _form_d_units(rec, res)
            return res
    res = ExtractResult(str(path), "xml")
    txt = BeautifulSoup(data, "xml").get_text("\n")
    txt = re.sub(r"\n\s*\n+", "\n", txt).strip()
    if txt:
        res.units.append(TextUnit(txt, {}, line_numbers=True))
    return res


def extract_edgar_submission(path: Path, text: str, opts: ExtractOptions) -> ExtractResult:
    """EDGAR full-text submission (.txt, SGML): extract every text/HTML
    <DOCUMENT>, parse Form D XML, skip uuencoded binaries and XBRL data files."""
    res = ExtractResult(str(path), "edgar_submission")
    header, docs = parse_sgml_submission(text)
    res.info["filer"] = header
    res.info["documents"] = []
    for d in docs:
        kind = classify_sgml_document(d)
        label = d.get("type") or "?"
        if d.get("filename"):
            label += f" ({d['filename']})"
        entry = {"type": d.get("type"), "sequence": d.get("sequence"),
                 "filename": d.get("filename"), "handling": kind}
        res.info["documents"].append(entry)
        if kind.startswith("skip"):
            continue
        base = {"doc": label}
        tracker = SectionTracker(exhibit=exhibit_label(d.get("type", "")))
        if kind == "form_d":
            body = strip_sgml_wrappers(d["body"]).strip()
            rec = parse_form_d_xml(body[body.find("<"):]) if "<" in body else None
            if rec:
                _form_d_units(rec, res, base)
                res.info["filer"] = {**header, **{k: v for k, v in res.info["filer"].items() if v}}
        elif kind == "html":
            facts = html_to_units(strip_sgml_wrappers(d["body"]), res, base, tracker)
            for k, v in facts.items():
                if k in ("name", "cik", "form_type") and v and not res.info["filer"].get(k):
                    res.info["filer"][k] = v
        else:
            body = d["body"].strip("\n")
            if body.strip():
                res.units.append(_sectioned_unit(body, base, tracker))
    res.info.pop("_table_offset", None)
    return res


# ------------------------------------------------------------ spreadsheets
def extract_xlsx(path: Path, opts: ExtractOptions) -> ExtractResult:
    import openpyxl
    from openpyxl.utils import get_column_letter

    res = ExtractResult(str(path), "xlsx")
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            for r, row in enumerate(ws.iter_rows(values_only=True), 1):
                # only text cells can contain company names (numbers/dates skipped)
                vals = [(v, {"cell": f"{get_column_letter(c)}{r}"})
                        for c, v in enumerate(row, 1) if isinstance(v, str)]
                u = _row_unit(vals, {"sheet": ws.title, "row": r})
                if u:
                    res.units.append(u)
        res.info["sheets"] = [ws.title for ws in wb.worksheets]
    finally:
        wb.close()
    return res


def extract_csv(path: Path, opts: ExtractOptions) -> ExtractResult:
    from openpyxl.utils import get_column_letter

    res = ExtractResult(str(path), "csv")
    txt = _read_text_file(path)
    delim = "\t" if path.suffix.lower() == ".tsv" else ","
    if delim == ",":
        try:
            delim = csv.Sniffer().sniff(txt[:8192], delimiters=",;\t|").delimiter
        except csv.Error:
            pass
    for r, row in enumerate(csv.reader(io.StringIO(txt), delimiter=delim), 1):
        vals = [(v, {"cell": f"{get_column_letter(c)}{r}", "col": c}) for c, v in enumerate(row, 1)]
        u = _row_unit(vals, {"row": r})
        if u:
            res.units.append(u)
    return res


# -------------------------------------------------------------------- PPTX
def _shape_texts(shapes) -> list[str]:
    out = []
    for sh in shapes:
        if getattr(sh, "shape_type", None) == 6 or hasattr(sh, "shapes"):  # group
            try:
                out.extend(_shape_texts(sh.shapes))
                continue
            except AttributeError:
                pass
        if getattr(sh, "has_text_frame", False) and sh.has_text_frame:
            t = sh.text_frame.text
            if t.strip():
                out.append(t)
        if getattr(sh, "has_table", False) and sh.has_table:
            for row in sh.table.rows:
                cells = [c.text for c in row.cells if c.text.strip()]
                if cells:
                    out.append(CELL_SEP.join(cells))
    return out


def extract_pptx(path: Path, opts: ExtractOptions) -> ExtractResult:
    from pptx import Presentation

    res = ExtractResult(str(path), "pptx")
    prs = Presentation(str(path))
    for i, slide in enumerate(prs.slides, 1):
        txt = "\n".join(_shape_texts(slide.shapes))
        if txt.strip():
            res.units.append(TextUnit(txt, {"slide": i}))
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text if slide.notes_slide.notes_text_frame else ""
            if notes.strip():
                res.units.append(TextUnit(notes, {"slide": i, "part": "notes"}))
    res.info["slides"] = len(prs.slides)
    return res


# ---------------------------------------------------------------- dispatch
def extract(path: str | Path, opts: ExtractOptions | None = None) -> ExtractResult:
    opts = opts or ExtractOptions()
    path = Path(path)
    ext = path.suffix.lower()
    if ext in PDF_EXT:
        return extract_pdf(path, opts)
    if ext in DOCX_EXT:
        return extract_docx(path, opts)
    if ext in HTML_EXT:
        return extract_html(path, opts)
    if ext in XML_EXT:
        return extract_xml(path, opts)
    if ext in XLSX_EXT:
        return extract_xlsx(path, opts)
    if ext in CSV_EXT:
        return extract_csv(path, opts)
    if ext in PPTX_EXT:
        return extract_pptx(path, opts)
    if ext in TEXT_EXT:
        return extract_text(path, opts)
    res = ExtractResult(str(path), "unsupported")
    if ext in LEGACY_EXT:
        res.warnings.append(f"legacy format {ext} not supported; convert to {LEGACY_EXT[ext]}")
    else:
        res.warnings.append(f"unsupported file type {ext or '(none)'}")
    return res
