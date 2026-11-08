"""Optional fuzzy matcher (rapidfuzz) over capitalised n-grams.

Only runs of capitalised tokens (allowing lower-case connectors such as "of",
"and" inside) that are not already claimed by the exact matcher are
considered, which keeps the candidate set small.  Each candidate n-gram is
compared with alias keys having a similar token count and the same first
letter (a cheap blocking step so the pass stays fast for large lists).
"""
from __future__ import annotations

from rapidfuzz import fuzz, process

from ..config import Config
from ..loader import Company
from ..mention import Mention
from ..variants import EXACT, AliasSpec
from .base import BaseMatcher, MatchContext

_HARD_GAP_CHARS = set(",;:!?()[]{}\"|/")
_ABBREV_DOT_OK = {"co", "inc", "ltd", "corp", "pvt", "st", "mr", "dr", "jr", "sr"}


class FuzzyMatcher(BaseMatcher):
    name = "fuzzy"

    def __init__(self, automaton, specs: list[AliasSpec], companies: list[Company],
                 cfg: Config, common: set[str], stop: set[str]):
        self.automaton = automaton
        self.specs = specs
        self.companies = companies
        self.cfg = cfg
        self.threshold = float(cfg.fuzzy_threshold)
        self.common = common
        self.stop = stop
        self.connectors = {c.lower() for c in cfg.connectors}
        self.legal = {c.lower() for c in cfg.legal_suffixes}
        key_to_sids: dict[str, list[int]] = {}
        for sid, sp in enumerate(specs):
            if sp.case_mode == EXACT:
                continue  # acronyms/tickers are never fuzzy-matched
            if len(sp.key) - sp.key.count(" ") < cfg.fuzzy_min_length:
                continue
            key_to_sids.setdefault(sp.key, []).append(sid)
        self.buckets: dict[tuple[int, str], tuple[list[str], list[list[int]]]] = {}
        for key, sids in key_to_sids.items():
            b = self.buckets.setdefault((key.count(" ") + 1, key[0]), ([], []))
            b[0].append(key)
            b[1].append(sids)

    def _hard_break(self, gap: str, prev_tok) -> bool:
        if "\n\n" in gap or any(c in _HARD_GAP_CHARS for c in gap):
            return True
        if "." in gap and len(prev_tok.cased) > 1 and prev_tok.key not in _ABBREV_DOT_OK:
            return True
        return False

    def _runs(self, ctx: MatchContext) -> list[list[int]]:
        toks = ctx.ntext.tokens
        runs: list[list[int]] = []
        cur: list[int] = []

        def flush():
            while cur and toks[cur[-1]].key in self.connectors:
                cur.pop()
            if cur:
                runs.append(list(cur))
            cur.clear()

        for idx, t in enumerate(toks):
            if cur and self._hard_break(ctx.ntext.gap(cur[-1], idx), toks[cur[-1]]):
                flush()
            first = t.cased[:1]
            if first.isupper() or (cur and (t.key in self.connectors or first.isdigit())):
                cur.append(idx)
            else:
                flush()
        flush()
        return runs

    def _take(self, ctx: MatchContext, out: list[Mention], i: int, j: int,
              best: dict[int, AliasSpec]) -> bool:
        """Claim tokens i..j.  A fuzzy candidate may replace shorter exact
        mentions strictly inside it when they are for the same company
        ("Reliance Industires" supersedes the exact alias hit "Reliance")."""
        if not any(ctx.covered[i:j + 1]):
            ctx.claim(i, j)
            return True
        ids = {self.companies[c].id for c in best}
        inside = []
        for m in list(ctx.mentions) + out:
            a, b = m.tok_span or (-1, -1)
            if b < i or a > j:
                continue
            if a < i or b > j or (a == i and b == j) or m.company_id not in ids:
                return False
            inside.append(m)
        if not inside:
            return False
        for m in inside:
            (ctx.mentions if m in ctx.mentions else out).remove(m)
        ctx.covered[i:j + 1] = b"\x01" * (j - i + 1)
        return True

    def find(self, ctx: MatchContext) -> list[Mention]:
        if not self.buckets:
            return []
        toks = ctx.ntext.tokens
        cands = []
        max_n = self.cfg.fuzzy_max_ngram
        for run in self._runs(ctx):
            L = len(run)
            for a in range(L):
                if toks[run[a]].key in self.connectors:
                    continue
                for n in range(1, min(max_n, L - a) + 1):
                    b = a + n - 1
                    if toks[run[b]].key in self.connectors:
                        continue
                    keys = [toks[x].key for x in run[a:b + 1]]
                    q = " ".join(keys)
                    if len(q) - (n - 1) < self.cfg.fuzzy_min_length:
                        continue
                    content = [k for k in keys if k not in self.connectors and k not in self.legal]
                    if not content or all(k in self.stop for k in content):
                        continue
                    if n == 1 and q in self.common:
                        continue
                    if self.automaton.exists(q):
                        continue  # exact key: handled (or deliberately rejected) by exact pass
                    best = None
                    for m in (n - 1, n, n + 1):
                        bucket = self.buckets.get((m, q[0]))
                        if not bucket:
                            continue
                        res = process.extractOne(q, bucket[0], scorer=fuzz.ratio,
                                                 score_cutoff=self.threshold)
                        if res and (best is None or res[1] > best[0]):
                            best = (res[1], bucket[1][res[2]])
                    if best:
                        cands.append((best[0], n, run[a], run[b], best[1]))
        cands.sort(key=lambda c: (-c[0], -c[1], c[2]))
        out: list[Mention] = []
        for score, _n, i, j, sids in cands:
            best: dict[int, AliasSpec] = {}
            for sid in sids:
                sp = self.specs[sid]
                best.setdefault(sp.company_idx, sp)
            if not self._take(ctx, out, i, j, best):
                continue
            for cidx, sp in best.items():
                out.append(ctx.make_mention(
                    self.companies[cidx], i, j, match_type="fuzzy", score=round(score, 1),
                    alias=sp.display, matcher=self.name, ambiguous=len(best) > 1))
        return out
