"""CompanyMatcher: the library entry point."""
from __future__ import annotations

import hashlib
import logging
import os
import pickle
import tempfile
import time
from pathlib import Path

import ahocorasick

from .config import Config
from .context import snippet
from .dedupe import prepare_companies
from .loader import Company, load_companies, load_companies_many, normalize_sheet_args
from .matchers.base import BaseMatcher, MatchContext
from .matchers.exact import BLOCK, ExactMatcher
from .matchers.fuzzy import FuzzyMatcher
from .mention import Mention, Passage, dedupe_identical
from .normalise import NormalizedText
from .variants import AliasSpec, Rejected, VariantGenerator

log = logging.getLogger(__name__)

CACHE_VERSION = "cf-2"  # bump when alias generation / automaton layout changes


def default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "company_finder"


class CompanyMatcher:
    """Find mentions of known companies in text.

    >>> m = CompanyMatcher.from_file("samples/companies_small.csv")
    >>> [x.canonical_name for x in m.find("Infosys's revenue grew.")]
    ['Infosys Limited']
    """

    def __init__(self, companies: list[Company], config: Config | None = None):
        t0 = time.perf_counter()
        self.config = config or Config()
        self.companies = companies
        gen = VariantGenerator(self.config)
        self.specs: list[AliasSpec] = []
        self.rejected: list[Rejected] = []
        for idx, c in enumerate(companies):
            specs, rej = gen.generate(c, idx)
            self.specs.extend(specs)
            self.rejected.extend(rej)

        table: dict[str, list[int]] = {}
        for sid, sp in enumerate(self.specs):
            table.setdefault(sp.key, []).append(sid)
        phrases = list(self.config.block_phrases)
        for c in companies:
            phrases.extend(c.exclude_phrases)
        self.block_keys = gen.block_keys(phrases)
        for k in self.block_keys:
            table.setdefault(k, []).append(BLOCK)

        A = ahocorasick.Automaton()
        for key, ids in table.items():
            A.add_word(key, (len(key), tuple(ids)))
        if len(A):
            A.make_automaton()
        self.automaton = A
        self.cik_index: dict[str, list[int]] = {}
        for idx, c in enumerate(companies):
            if getattr(c, "cik", None):
                self.cik_index.setdefault(c.cik, []).append(idx)
        self.build_seconds = time.perf_counter() - t0
        self.source_path: str | None = None
        self.cache_info: dict = {"cache_hit": False}
        self._init_runtime()

    # ------------------------------------------------------------ pickling
    def _init_runtime(self):
        self._exact = ExactMatcher(self.automaton, self.specs, self.companies, self.config)
        self._fuzzy: FuzzyMatcher | None = None
        self._extra: list[BaseMatcher] = []

    def __getstate__(self):
        d = self.__dict__.copy()
        for k in ("_exact", "_fuzzy", "_extra"):
            d.pop(k, None)
        return d

    def __setstate__(self, d):
        self.__dict__.update(d)
        self._init_runtime()

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "wb") as fh:
            pickle.dump(self, fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)

    @staticmethod
    def load(path: str | Path) -> "CompanyMatcher":
        # Only load caches you created yourself: pickle executes code on load.
        with open(path, "rb") as fh:
            obj = pickle.load(fh)
        if not isinstance(obj, CompanyMatcher):
            raise TypeError(f"{path} is not a CompanyMatcher cache")
        return obj

    # --------------------------------------------------------- construction
    @classmethod
    def from_file(cls, path, config: Config | None = None, *,
                  cache_dir: str | Path | None = None, use_cache: bool = True,
                  sheets=None, dedupe: bool = False,
                  exclude: list[str] | None = None) -> "CompanyMatcher":
        """Build from one company file or a list of them (CSV/JSON/TXT/XLSX), using a
        pickle cache keyed by the SHA-256 of the file contents + config + version.

        ``sheets`` limits ``.xlsx`` / ``.xlsm`` workbooks (see ``load_companies``).
        ``dedupe`` collapses same-spelling rows (body + suffix class) before the
        automaton is built. ``exclude`` drops companies whose canonical name or
        any alias soft-matches, also before matching and before LLM ranking.
        """
        config = config or Config()
        paths = [Path(p) for p in path] if isinstance(path, (list, tuple)) else [Path(path)]
        path = ",".join(str(p) for p in paths)
        sheet_names = normalize_sheet_args(sheets)
        exclude = [e for e in (exclude or []) if e and str(e).strip()]
        cache_path = None
        if use_cache:
            h = hashlib.sha256()
            h.update(CACHE_VERSION.encode())
            for p in paths:
                h.update(p.read_bytes())
                h.update(b"\x00FILE\x00")
            if sheet_names:
                h.update(b"\x00SHEETS\x00")
                h.update("\x00".join(sheet_names).encode())
            h.update(b"\x00DEDUPE\x01" if dedupe else b"\x00DEDUPE\x00")
            if exclude:
                h.update(b"\x00EXCLUDE\x00")
                for name in exclude:
                    h.update(str(name).encode())
                    h.update(b"\x00")
            h.update(config.fingerprint().encode())
            cache_path = Path(cache_dir or default_cache_dir()) / f"matcher-{h.hexdigest()[:32]}.pkl"
            if cache_path.exists():
                t0 = time.perf_counter()
                try:
                    m = cls.load(cache_path)
                    m.config = config  # runtime-only settings (fuzzy, context) may differ
                    m._init_runtime()
                    m.cache_info = {"cache_hit": True, "cache_path": str(cache_path),
                                    "load_seconds": time.perf_counter() - t0}
                    m.source_path = str(path)
                    return m
                except Exception as exc:  # corrupt / incompatible cache -> rebuild
                    log.warning("Ignoring unreadable cache %s: %s", cache_path, exc)
        companies = prepare_companies(
            load_companies_many(paths, sheets=sheet_names),
            dedupe=dedupe, exclude=exclude)
        m = cls(companies, config)
        m.source_path = str(path)
        m.cache_info = {"cache_hit": False, "build_seconds": m.build_seconds}
        if cache_path is not None:
            try:
                m.save(cache_path)
                m.cache_info["cache_path"] = str(cache_path)
            except OSError as exc:
                log.warning("Could not write cache %s: %s", cache_path, exc)
        return m

    # -------------------------------------------------------------- matchers
    def add_matcher(self, matcher: BaseMatcher) -> None:
        """Register an extra matcher run after exact (and fuzzy) matching.

        Future extension point (e.g. a redesigned Phase-2 matcher over
        ``unmatched_passages()``).
        """
        self._extra.append(matcher)

    @property
    def matchers(self) -> list[BaseMatcher]:
        ms: list[BaseMatcher] = [self._exact]
        if self.config.fuzzy:
            if self._fuzzy is None or self._fuzzy.threshold != float(self.config.fuzzy_threshold):
                gen = VariantGenerator(self.config)
                self._fuzzy = FuzzyMatcher(self.automaton, self.specs, self.companies,
                                           self.config, gen.common, gen.stop)
            ms.append(self._fuzzy)
        return ms + self._extra

    # ------------------------------------------------------------------ find
    def find(self, text: str, *, with_context: bool = True) -> list[Mention]:
        """Return mentions in ``text`` (offsets refer to ``text``)."""
        return self.find_with_context(text, with_context=with_context)[0]

    def find_with_context(self, text: str, *, with_context: bool = True
                          ) -> tuple[list[Mention], MatchContext]:
        ctx = MatchContext(text, NormalizedText(text))
        if ctx.ntext.tokens:
            for m in self.matchers:
                ctx.mentions.extend(m.find(ctx))
        ctx.mentions = dedupe_identical(ctx.mentions)
        ctx.mentions.sort(key=lambda m: (m.char_start, -m.char_end, m.company_id))
        if with_context:
            w = self.config.context_max_chars
            for m in ctx.mentions:
                m.context = snippet(text, m.char_start, m.char_end, w)
        return ctx.mentions, ctx

    def unmatched_passages(self, text: str) -> list[Passage]:
        """Sentences of ``text`` without any detected mention (future extension hook)."""
        _, ctx = self.find_with_context(text, with_context=False)
        return ctx.unmatched_passages()

    # ------------------------------------------------------------- inspection
    def alias_table(self) -> list[dict]:
        rows = []
        for sp in self.specs:
            c = self.companies[sp.company_idx]
            rows.append({"company_id": c.id, "canonical_name": c.canonical_name,
                         "variant": sp.display, "key": sp.key, "source": sp.source,
                         "case_mode": sp.case_mode, "origin": sp.origin,
                         "common_word": sp.common_word})
        return rows

    def rejected_table(self) -> list[dict]:
        return [{"company_id": self.companies[r.company_idx].id, "variant": r.variant,
                 "origin": r.origin, "reason": r.reason} for r in self.rejected]

    def __repr__(self) -> str:
        return (f"CompanyMatcher(companies={len(self.companies)}, aliases={len(self.specs)}, "
                f"block_phrases={len(self.block_keys)})")
