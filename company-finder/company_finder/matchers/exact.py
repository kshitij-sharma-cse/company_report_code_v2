"""Aho-Corasick exact/alias matcher with word boundaries and longest-match."""
from __future__ import annotations

import logging
import re

from ..config import DEFAULT_NAME_TAIL_SKIP_WORDS, Config
from ..dedupe import company_matches_surface, legal_form, surface_form
from ..loader import Company
from ..mention import Mention
from ..normalise import NormalizedText
from ..variants import CAPITALISED, EXACT, TICKER, AliasSpec
from .base import BaseMatcher, MatchContext

log = logging.getLogger(__name__)

BLOCK = -1
_SENT_END_CHARS = set(".!?:\"'\u2022*\u00b7\u25aa")
_EXT_GAP_FIRST = re.compile(r"[ \t,]*")
_EXT_GAP_NEXT = re.compile(r"[ \t,.]*")
_TICKER_GAP = re.compile(r"\s*[:\-\u2013]?\s*")
_ROMAN = re.compile(r"^(?:II|III|IV|V|VI|VII|VIII|IX|X|XI|XII)$")
_SPACE_GAP = re.compile(r"[ \t]+")


class ExactMatcher(BaseMatcher):
    name = "exact"

    def __init__(self, automaton, specs: list[AliasSpec], companies: list[Company], cfg: Config):
        self.automaton = automaton
        self.specs = specs
        self.companies = companies
        self.cfg = cfg
        self.connectors = {c.lower() for c in cfg.connectors}
        self.stop = cfg.stop_set()
        self.ext = {s.lower() for s in cfg.extension_suffixes}
        self.legal = {s.lower() for s in cfg.legal_suffixes}
        self.entity_words = {s.lower() for s in cfg.entity_words}
        self.common = None  # loaded lazily (kept out of pickles)

    # -------------------------------------------------------------- checks
    def _case_ok(self, spec: AliasSpec, nt: NormalizedText, i: int, j: int) -> bool:
        if spec.case_mode == TICKER:
            k = i + spec.skip_tokens  # prefix any case; ticker exact; "NYSE: C" / "NYSE - C"
            if not _TICKER_GAP.fullmatch(nt.gap(k - 1, k)):
                return False
            return all(t.cased == c for t, c in zip(nt.tokens[k:j + 1], spec.cased[spec.skip_tokens:]))
        if spec.case_mode == EXACT:
            for t, c in zip(nt.tokens[i:j + 1], spec.cased):
                if t.cased != c and not (t.key == "and" and c.lower() == "and"):
                    return False
            return True
        if spec.case_mode == CAPITALISED:
            for t in nt.tokens[i:j + 1]:
                # connectors and generic stop words may be lower-case ("the Tata group")
                if t.key in self.connectors or (j > i and t.key in self.stop):
                    continue
                ch = t.cased[0]
                if ch.isalpha() and not ch.isupper():
                    return False
        return True

    def _weak_context(self, nt: NormalizedText, i: int, j: int) -> bool:
        """Capitalisation carries no signal: sentence/line start or ALL CAPS."""
        prev = nt.preceding(i)
        if i == 0 or "\n" in prev:
            return True
        p = prev.rstrip()
        if p and p[-1] in _SENT_END_CHARS:
            return True
        toks = nt.tokens
        span_caps = all(t.cased.isupper() for t in toks[i:j + 1])
        if span_caps:
            neigh = [toks[k] for k in (i - 1, j + 1) if 0 <= k < len(toks)]
            if any(len(t.cased) > 1 and t.cased.isupper() for t in neigh):
                return True
        if self.cfg.title_case_weak_context:
            # Title Case run (headings): "Our Target Market", "Target Returns May
            # Not Be Achieved".  A single capitalised neighbour is not enough -
            # "Microsoft Azure and", "Apple Pay and" are ordinary prose.
            def cap(k, a, b):  # token k capitalised and directly adjacent (gap a..b)
                return (0 <= k < len(toks) and toks[k].cased[:1].isupper()
                        and re.fullmatch(r"[ \t]*", nt.gap(a, b)) is not None)
            if j + 1 < len(toks) and self._title_word(toks[j + 1]) and cap(j + 1, j, j + 1):
                if cap(j + 2, j + 1, j + 2) or (i >= 1 and cap(i - 1, i - 1, i)):
                    return True
            if i >= 1 and self._title_word(toks[i - 1]) and cap(i - 1, i - 1, i):
                pp = nt.preceding(i - 1)
                prev_is_start = i - 1 == 0 or "\n" in pp or (pp.rstrip()[-1:] in _SENT_END_CHARS)
                if not prev_is_start and (cap(i - 2, i - 2, i - 1) or cap(j + 1, j, j + 1)):
                    return True
        return False

    def _lowercase_common(self, nt: NormalizedText, i: int, j: int) -> bool:
        """'the ironclad fencing contract' - a multi-word name written entirely in
        lower case that contains ordinary words is probably not the name."""
        toks = nt.tokens[i:j + 1]
        if any(t.cased[:1].isupper() or t.cased[:1].isdigit() for t in toks):
            return False
        return any(t.key in self.common and t.key not in self.connectors for t in toks)

    def _title_word(self, t) -> bool:
        return (t.cased[:1].isupper() and not t.cased.isupper() and t.key in self.common
                and t.key not in self.connectors and t.key not in self.ext
                and t.key not in self.legal)

    def _longer_entity(self, ctx: MatchContext, j: int) -> int:
        """Index of the last token of a trailing entity-structure run
        ("... Holdings II, LLC"), or j if there is none."""
        toks = ctx.ntext.tokens
        k, found = j, False
        while k + 1 < len(toks) and not ctx.covered[k + 1]:
            t = toks[k + 1]
            if not re.fullmatch(r"[ \t,]*", ctx.ntext.gap(k, k + 1)):
                break
            if (t.key in self.entity_words and t.cased[:1].isupper()) or _ROMAN.match(t.cased):
                found = True
                k += 1
                continue
            if found and (t.key in self.ext or t.key in self.legal) and t.cased[:1].isupper():
                k += 1
                continue
            break
        return k if found else j

    def _name_tail(self, ctx: MatchContext, i: int, j: int, cidxs) -> tuple[str, bool]:
        """(longer name, drop) for a match that may end a longer organisation name
        ("A Work of Faith Community Outreach Corp" for "Outreach Corp"); ("", False)
        when the match starts a name.

        A candidate: the token right before the match is a Title-case word on the
        same line, separated by spaces only, that is not a function word, legal
        suffix or another claimed match, and no name of the company contains that
        word pair (an alias covers the longer name).

        ``drop`` is True only when layout cannot explain the run-in:
        - the gap is exactly one space (a wider gap is a column gap);
        - the text was not line-joined by the extractor (PDF: a heading line runs
          into the next line);
        - the line or cell up to the match is not a heading run-in: Title-case
          words, commas and "&"/"and" only, no article or other connector
          ("Application Software Databricks, Inc.", "Health Care Providers &
          Services Get Heal, Inc." are headings; "A Work of Faith Community ..."
          is not);
        - the match ends in a legal suffix or the longer name has two or more
          words before it ("Microsoft Databricks partnership" is only a hint).
        A candidate that is not dropped is returned as a hint for the LLM pass."""
        if not getattr(self.cfg, "drop_name_tail_matches", False) or i == 0:
            return "", False
        toks, nt = ctx.ntext.tokens, ctx.ntext
        p = toks[i - 1]
        gap = nt.gap(i - 1, i)
        if ctx.covered[i - 1] or not _SPACE_GAP.fullmatch(gap):
            return "", False
        if not (p.cased[:1].isupper() and not p.cased.isupper()):
            return "", False  # lower-case word, ALL CAPS heading, digit
        skip = getattr(self, "tail_skip", None)
        if skip is None:
            skip = self.tail_skip = {w.lower() for w in getattr(
                self.cfg, "name_tail_skip_words", DEFAULT_NAME_TAIL_SKIP_WORDS)}
        if p.key in skip or p.key in self.legal or p.key in self.ext:
            return "", False
        pair = f" {p.key} {toks[i].key} "
        if any(sp.company_idx in cidxs and pair in f" {sp.key} " for sp in self.specs):
            return "", False
        k = i - 1  # walk back over the rest of the longer name
        while k > 0 and _SPACE_GAP.fullmatch(nt.gap(k - 1, k)) and (
                toks[k - 1].cased[:1].isupper() or toks[k - 1].key in self.connectors):
            k -= 1
        while k < i - 1 and toks[k].key in self.connectors:
            k += 1
        s0, _ = nt.orig_span(k, i - 1)
        m0, _ = nt.orig_span(i, i)
        _, e0 = nt.orig_span(j, j)
        longer = ctx.text[s0:e0]
        drop = (gap == " " and not ctx.joined_lines
                and not self._heading_run_in(ctx.text[:m0])
                and (toks[j].key in self.legal or toks[j].key in self.ext or i - k >= 2))
        return longer, drop

    def _heading_run_in(self, before: str) -> bool:
        """Is the text from the start of the line or cell up to the match a heading
        (sector / industry label) rather than the start of a longer name?"""
        segs = re.split(r"(\n| \| |\t| {2,})", before)
        line = segs[-1].strip()
        if len(segs) > 2 and segs[-2] == "\n":
            # a line break inside wrapped prose ("... of Acadia Parish and the\nCommunity
            # Outreach Corporation") is not the start of a heading: the previous line ends
            # mid-sentence: a lower-case word, an initial or short title ("D.", "St."), a
            # comma, or a capitalised word on a line that has ordinary lower-case words
            prev = segs[-3].rstrip()
            if re.search(r"(?:\b[a-z][\w'’-]*|\b[A-Z][a-z]?\.|[,&\-])$", prev) or (
                    re.search(r"[^\W\d_]$", prev)
                    and any(w[:1].islower() and w.lower() not in self.connectors
                            for w in re.findall(r"[^\W\d_]+", prev))):
                return False
        words = re.findall(r"[^\W_]+(?:['.][^\W_]+)*", line)
        if not words or re.search(r"[^\w\s,&/'.\-]", line):
            return False
        articles = {"a", "an", "the"}
        for w in words:
            lw = w.lower()
            if lw == "and":
                continue
            if lw in articles or lw in self.connectors or not w[:1].isupper():
                return False
        return True

    def _extend(self, ctx: MatchContext, j: int) -> int:
        """Absorb trailing legal suffixes ("... Industries Ltd.")."""
        if not self.cfg.extend_legal_suffixes:
            return j
        toks = ctx.ntext.tokens
        k = j
        while k + 1 < len(toks) and not ctx.covered[k + 1]:
            gap = ctx.ntext.gap(k, k + 1)
            gap_re = _EXT_GAP_FIRST if k == j else _EXT_GAP_NEXT
            if not gap_re.fullmatch(gap):
                break
            t = toks[k + 1]
            if t.key in self.ext and t.cased[:1].isupper():
                k += 1
                continue
            # dotted initials: "L.P.", "L.L.C.", "N.A."
            n = 0
            while (k + 1 + n < len(toks) and len(toks[k + 1 + n].cased) == 1
                   and toks[k + 1 + n].cased.isupper() and not ctx.covered[k + 1 + n] and n < 3
                   and (n == 0 or ctx.ntext.gap(k + n, k + 1 + n) in (".", ". ", "")) ):
                n += 1
            if n >= 2 and "".join(x.key for x in toks[k + 1:k + 1 + n]) in self.ext:
                k += n
                continue
            if (t.key == "and" and k + 2 < len(toks) and not ctx.covered[k + 2]
                    and toks[k + 2].key in ("co", "company") and toks[k + 2].cased[:1].isupper()
                    and _EXT_GAP_NEXT.fullmatch(ctx.ntext.gap(k + 1, k + 2))):
                k += 2
                continue
            break
        return k

    # ---------------------------------------------------------------- find
    def _ensure_common(self):
        if self.common is None:
            from ..variants import VariantGenerator
            self.common = VariantGenerator(self.cfg).common

    def __getstate__(self):
        d = self.__dict__.copy()
        d["common"] = None
        return d

    def find(self, ctx: MatchContext) -> list[Mention]:
        self._ensure_common()
        nt = ctx.ntext
        key = nt.key
        if not key:
            return []
        klen_total = len(key)
        cands = []
        for end, (klen, ids) in self.automaton.iter(key):
            start = end - klen + 1
            if start > 0 and key[start - 1] != " ":
                continue
            if end + 1 < klen_total and key[end + 1] != " ":
                continue
            i = nt.tok_at_start.get(start)
            j = nt.tok_at_end.get(end + 1)
            if i is None or j is None:
                continue
            block = False
            ok = []
            for sid in ids:
                if sid == BLOCK:
                    block = True
                elif self._case_ok(self.specs[sid], nt, i, j):
                    ok.append(sid)
            if block or ok:
                cands.append((klen, j - i + 1, i, j, block, ok))
        # longest match wins; ties -> leftmost; blockers win exact ties
        cands.sort(key=lambda c: (-c[0], -c[1], c[2], not c[4]))

        out: list[Mention] = []
        for _klen, _n, i, j, block, ok in cands:
            if not ctx.claim(i, j):
                continue
            if block:
                continue
            j2 = self._extend(ctx, j)
            if j2 > j:
                ctx.claim(j + 1, j2)
            j3 = self._longer_entity(ctx, j2)
            note = ""
            if j3 > j2:
                s0, e0 = nt.orig_span(i, j3)
                note = f"part of longer entity name: {ctx.text[s0:e0]!r}"
            # best spec per company (canonical > alias)
            best: dict[int, AliasSpec] = {}
            for sid in ok:
                sp = self.specs[sid]
                cur = best.get(sp.company_idx)
                if cur is None or (cur.source != "canonical" and sp.source == "canonical"):
                    best[sp.company_idx] = sp
            # Several companies sharing a stripped stem (BlackRock → Inc and
            # Limited). Prefer companies whose *canonical* legal_form matches
            # the surface actually written. That stops Master rows that list
            # bare "BlackRock" as an alias on Inc/Limited/(US) from fan-out
            # when an unsuffixed BLACKROCK sibling is also in the hit set.
            # Fall back to any declared name (canonical or alias) matching the
            # surface. A single company is left alone so "Infosys" still hits
            # "Infosys Limited".
            if len(best) > 1:
                s_surf, e_surf = nt.orig_span(i, j2)
                surface = surface_form(ctx.text, s_surf, e_surf)
                canon = {cidx: sp for cidx, sp in best.items()
                         if legal_form(self.companies[cidx].canonical_name) == surface}
                if canon:
                    best = canon
                else:
                    best = {cidx: sp for cidx, sp in best.items()
                            if company_matches_surface(self.companies[cidx], surface)}
            if not best:
                continue
            longer, drop = "", False
            if not any(self.specs[s].source == "ticker" for s in ok):
                longer, drop = self._name_tail(ctx, i, j2, set(best))
            if drop:
                for cidx, sp in best.items():
                    m = ctx.make_mention(
                        self.companies[cidx], i + sp.skip_tokens, j2, match_type=sp.match_type,
                        score=0.0, alias=sp.display, matcher=self.name, ambiguous=len(best) > 1)
                    m.note = f"tail of a longer name: {longer!r}"
                    m.extras = {"dropped_by": "name_tail_rule", "name_tail": longer}
                    ctx.dropped.append(m)
                log.debug("dropped %r: tail of longer name %r", m.matched_text, longer)
                continue
            ambiguous = len(best) > 1
            weak = None
            for cidx, sp in best.items():
                score = 100.0
                if sp.common_word:
                    if weak is None:
                        weak = self._weak_context(nt, i, j)
                    if weak:
                        score = float(self.cfg.ambiguous_weak_context_score)
                if note:
                    score = min(score, float(self.cfg.longer_entity_score))
                elif j > i and self._lowercase_common(nt, i, j):
                    score = min(score, float(self.cfg.ambiguous_weak_context_score))
                    note = "all lower-case, contains common words"
                m = ctx.make_mention(
                    self.companies[cidx], i + sp.skip_tokens, j2, match_type=sp.match_type,
                    score=score, alias=sp.display, matcher=self.name, ambiguous=ambiguous)
                m.note = note
                if longer:  # not dropped (layout may explain it): a hint for --llm-disambiguate
                    m.extras = {**(m.extras or {}), "name_tail": longer}
                out.append(m)
        return out
