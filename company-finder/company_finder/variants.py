"""Automatic alias/variant generation and false-positive filtering.

For every name string in the company file (canonical name, aliases, ticker,
case-sensitive aliases) we produce a set of *variants*, each a list of cased
tokens (see ``normalise.py``), then filter them:

* legal suffixes stripped from the end ("Infosys Limited" -> "Infosys");
  secondary suffixes (Holdings/Group) only when >= 2 meaningful words remain;
  leading "The" dropped;
* possessives ("McDonald's" -> "McDonalds", "McDonald");
* parenthetical parts removed ("Hindustan Unilever Ltd (HUL)" -> core name);
* initials joined ("J.P. Morgan" -> "JP Morgan", "JPMorgan" -> "JP Morgan").
  Glued acronyms are NOT split into spaced initials ("TS" does not become "T S");
* a single common-word bare stem is not auto-created by stripping a suffix
  ("TARGET CORPORATION" does not become "Target"). An alias the file lists as
  that one word is kept;
* stop-list: aliases made only of generic words are dropped;
* minimum length;
* case policy: explicit case-sensitive aliases, tickers and short all-caps
  acronyms need an exact-case match; single common English words ("Apple",
  "Shell", "Axis") need a capitalised match.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Config
from .loader import Company
from .normalise import alias_tokens, key_of

INSENSITIVE, CAPITALISED, EXACT = "insensitive", "capitalised", "exact"
TICKER = "ticker"  # exchange prefix (any case) + ticker (exact case)
_CASE_RANK = {INSENSITIVE: 0, CAPITALISED: 1, EXACT: 2, TICKER: 3}


@dataclass(frozen=True, slots=True)
class AliasSpec:
    company_idx: int
    key: str                 # lower-case, space-joined tokens
    cased: tuple[str, ...]   # tokens as written (for exact-case checks)
    source: str              # 'canonical' | 'alias' | 'ticker'
    case_mode: str           # 'insensitive' | 'capitalised' | 'exact'
    origin: str              # raw string in the company file it came from
    common_word: bool = False
    skip_tokens: int = 0     # leading tokens not reported (exchange prefix)

    @property
    def match_type(self) -> str:
        if self.source == "ticker":
            return "ticker"
        return "exact" if self.source == "canonical" else "alias"

    @property
    def display(self) -> str:
        return " ".join(self.cased)


@dataclass(frozen=True, slots=True)
class Rejected:
    company_idx: int
    variant: str
    origin: str
    reason: str


_PAREN_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*")
_POSS_RE = re.compile(r"(?<=\w)['\u2019]s\b")
_CAMEL_RE = re.compile(r"^([A-Z]{2,})([A-Z][a-z]+)$")
_INNER_CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z][a-z])")
# "Acme Buyer, Inc. (d/b/a Acme Software)", "X LLC f/k/a Y LLC", "formerly Z"
_AKA_RE = re.compile(
    r"[\(\[,;]?\s*\b(?:d\s*/\s*b\s*/\s*a|f\s*/\s*k\s*/\s*a|a\s*/\s*k\s*/\s*a|n\s*/\s*k\s*/\s*a"
    r"|doing business as|formerly known as|also known as|now known as|formerly"
    r"|(?<=[\(\[])\s*(?:dba|fka|aka))\b[.:]?\s+(?P<name>[^()\[\];]+?)\s*[\)\]]?\s*(?=$|[;(\[])",
    re.I)


def split_aka(raw: str) -> tuple[str, list[str]]:
    """Split "Main Name (d/b/a Other)" into ("Main Name", ["Other"])."""
    akas = [m.group("name").strip(" ,") for m in _AKA_RE.finditer(raw)]
    if not akas:
        return raw, []
    main = _AKA_RE.sub(" ", raw).strip(" ,;")
    return (main or raw), [a for a in akas if a]


_ROMAN_RE = re.compile(r"(?:X{0,3})(?:IX|IV|V?I{0,3})")



def _listed_as_single_word(raw: str, tok: str) -> bool:
    """True when ``raw`` itself is this one word (explicit name or alias)."""
    origin = alias_tokens(raw)
    return len(origin) == 1 and origin[0].lower() == tok.lower()


class VariantGenerator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.legal = {s.lower() for s in cfg.legal_suffixes}
        self.secondary = {s.lower() for s in cfg.secondary_suffixes}
        self.connectors = {s.lower() for s in cfg.connectors}
        self.stop = cfg.stop_set()
        self.common = cfg.common_words()

    # ------------------------------------------------------------ helpers
    def _forms(self, raw: str) -> list[str]:
        forms = [raw]
        no_paren = _PAREN_RE.sub(" ", raw).strip()
        if no_paren and no_paren != raw:
            forms.append(no_paren)
        for f in list(forms):
            if _POSS_RE.search(f):
                forms.append(_POSS_RE.sub("s", f))
                forms.append(_POSS_RE.sub("", f))
        return forms

    @staticmethod
    def _join_initials(toks: list[str]) -> list[str]:
        out: list[str] = []
        run: list[str] = []
        for t in toks + [""]:
            if len(t) == 1 and t.isalpha():
                run.append(t)
                continue
            if len(run) >= 2:
                out.append("".join(run))
            else:
                out.extend(run)
            run = []
            if t:
                out.append(t)
        return out

    @staticmethod
    def _split_acronyms(toks: list[str]) -> list[str]:
        """Split glued acronyms into letters. Not used for auto-aliases.

        ``TS CORPORATION`` used to become ``T S`` and match ``T S Hale``.
        Dotted names already tokenise to separate letters (``J.P.`` -> J, P).
        A Master alias written as spaced initials (``T S``) is kept as written.
        """
        out = []
        for t in toks:
            if (2 <= len(t) <= 4 and t.isalpha() and t.isupper()
                    and not _ROMAN_RE.fullmatch(t)):  # "II" is a numeral, not "I.I."
                out.extend(list(t))
            else:
                out.append(t)
        return out

    @staticmethod
    def _split_camel(toks: list[str]) -> list[str]:
        out = []
        for t in toks:
            m = _CAMEL_RE.match(t)
            if m:
                out.extend([m.group(1), m.group(2)])
            else:
                out.append(t)
        return out

    @staticmethod
    def _split_inner_camel(toks: list[str]) -> list[str]:
        out = []
        for t in toks:
            out.extend(_INNER_CAMEL_RE.split(t) if t[:1].isupper() else [t])
        return out

    def _joined(self, toks: list[str]) -> list[str] | None:
        """"Exxon Mobil" -> ["ExxonMobil"] (2-3 alphabetic words, no connectors)."""
        if not 2 <= len(toks) <= 3:
            return None
        if any(not t.isalpha() or len(t) < 3 or t.lower() in self.connectors
               or t.lower() in self.legal for t in toks):
            return None
        return ["".join(toks)]

    def _strip_legal(self, toks: list[str]) -> list[str]:
        t = list(toks)
        changed = True
        while changed:
            changed = False
            while len(t) > 1 and t[-1].lower() in self.legal:
                t.pop()
                changed = True
            while changed and len(t) > 1 and t[-1].lower() in ("and", "of", "the"):
                t.pop()
        return t

    def _strip_leading_the(self, toks: list[str]) -> list[str]:
        return toks[1:] if len(toks) > 1 and toks[0].lower() == "the" else toks

    def _content(self, toks) -> list[str]:
        return [t.lower() for t in toks
                if t.lower() not in self.connectors and t.lower() not in self.legal]

    # --------------------------------------------------------------- main
    def generate(self, company: Company, idx: int) -> tuple[list[AliasSpec], list[Rejected]]:
        cfg = self.cfg
        base_raws: list[tuple[str, str, bool]] = [(company.canonical_name, "canonical", False)]
        base_raws += [(a, "alias", False) for a in company.aliases]
        base_raws += [(a, "alias", True) for a in company.case_sensitive_aliases]
        raws: list[tuple[str, str, bool]] = []
        for raw, source, forced in base_raws:
            main, akas = split_aka(raw)
            raws.append((main, source, forced))
            raws += [(a, "alias", forced) for a in akas]

        specs: dict[tuple, AliasSpec] = {}
        rejected: list[Rejected] = []

        for raw, source, forced_exact in raws:
            # (tokens, level) where level in full/core/derived
            variants: list[tuple[list[str], str]] = []
            for form in self._forms(raw):
                base = alias_tokens(form)
                if not base:
                    continue
                bases = [base]
                if cfg.join_initials:
                    bases.append(self._join_initials(base))
                if cfg.split_camel_acronyms:
                    bases.append(self._split_camel(base))
                if cfg.split_camel_acronyms:
                    inner = self._split_inner_camel(base)
                    if inner != base:
                        variants.append((inner, "derived"))
                for b in bases:
                    variants.append((b, "full"))
                    core = self._strip_leading_the(self._strip_legal(b))
                    if core != b:
                        variants.append((core, "core"))
                    if core and core[-1].lower() in self.secondary:
                        sec = self._strip_legal(core[:-1])
                        if len(self._content(sec)) >= 2:
                            variants.append((sec, "derived"))
                    j = self._joined(core)
                    if j:
                        variants.append((j, "derived"))
            # initials joined/split versions of every variant
            extra = []
            for toks, level in variants:
                if cfg.join_initials:
                    # Join dotted initials ("J.P." -> "JP"). Do NOT split a glued
                    # acronym into spaced letters: "TS" -> "T S" matched "T S Hale".
                    extra.append((self._join_initials(toks), level))
            variants += extra

            raw_toks = alias_tokens(raw)
            auto_acronym = (
                cfg.auto_case_sensitive_acronym_max_len > 0
                and len(raw_toks) == 1
                and raw_toks[0].isupper()
                and len(raw_toks[0]) <= cfg.auto_case_sensitive_acronym_max_len
            )
            self._emit(variants, idx, raw, source, forced_exact, auto_acronym, specs, rejected)

        self._emit_tickers(company, idx, specs, rejected)
        return self._finalise(specs), rejected

    def _emit(self, variants, idx, raw, source, forced_exact, auto_acronym, specs, rejected):
        cfg = self.cfg
        seen_keys = set()
        for toks, level in variants:
            key = key_of(toks)
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            disp = " ".join(toks)
            content = self._content(toks)
            if not content:
                rejected.append(Rejected(idx, disp, raw, "only legal suffixes/connectors"))
                continue
            if all(c in self.stop for c in content):
                if len(content) == 1:
                    rejected.append(Rejected(idx, disp, raw, "generic stop-list word"))
                    continue
                if level == "derived":
                    rejected.append(Rejected(idx, disp, raw, "only generic stop-list words"))
                    continue

            length = sum(len(t) for t in toks)
            max_acr = cfg.auto_case_sensitive_acronym_max_len
            letters = [t for t in toks if t.lower() != "and"]
            exact = forced_exact or auto_acronym or (
                # an acronym-only variant: "ITC" from "ITC Limited", "AT&T", "J&J"
                max_acr > 0 and letters and all(t.isupper() for t in letters)
                and sum(len(t) for t in letters) <= max_acr)
            min_len = cfg.min_case_sensitive_alias_length if exact else cfg.min_alias_length
            if length < min_len:
                rejected.append(Rejected(idx, disp, raw, f"shorter than {min_len} chars"))
                continue
            # "Target" from "TARGET CORPORATION" is a common English word.
            # Capitalisation is not enough (``score Target:``). Drop the auto
            # bare stem. Keep it when the file lists that one word as the name
            # or as an alias / case-sensitive alias.
            if (len(toks) == 1 and toks[0].lower() in self.common
                    and not _listed_as_single_word(raw, toks[0])):
                rejected.append(Rejected(
                    idx, disp, raw,
                    "auto common-word bare stem (list the word as an alias to keep it)"))
                continue

            common_word = False
            if exact:
                mode = EXACT
            elif len(toks) == 1 and cfg.single_word_case_policy != "none" and (
                    cfg.single_word_case_policy == "all" or toks[0].lower() in self.common):
                mode = CAPITALISED
                common_word = toks[0].lower() in self.common
            elif (len(content) >= 2 and cfg.multi_word_common_case_policy == "capitalised"
                  and all(c in self.common for c in content)):
                mode = CAPITALISED   # "Home Depot", "General Motors", "Capital One"
            else:
                mode = INSENSITIVE

            spec = AliasSpec(idx, key, tuple(toks), source, mode, raw, common_word)
            self._add(specs, spec)

    @staticmethod
    def _add(specs: dict, spec: AliasSpec) -> None:
        dkey = (spec.key, spec.case_mode, spec.cased if spec.case_mode in (EXACT, TICKER) else None)
        old = specs.get(dkey)
        if old is None or (old.source != "canonical" and spec.source == "canonical"):
            specs[dkey] = spec

    def _emit_tickers(self, company: Company, idx: int, specs: dict, rejected: list) -> None:
        """Tickers: exact-case free-standing alias when safe (>= free_ticker_min_length
        and not a common word); always after an exchange prefix ("NYSE: C")."""
        cfg = self.cfg
        prefixes = {key_of(alias_tokens(p)): alias_tokens(p) for p in cfg.exchange_prefixes}
        free = [(t, True) for t in company.tickers]
        free += [(t, False) for t in company.exchange_only_tickers]
        for tk, allow_free in free:
            toks = alias_tokens(tk)
            if not toks:
                continue
            letters = "".join(toks)
            if allow_free:
                if len(letters) < cfg.free_ticker_min_length:
                    rejected.append(Rejected(idx, tk, tk, "ticker too short for free-standing use "
                                                        "(still matched after an exchange prefix)"))
                elif letters.lower() in self.common:
                    rejected.append(Rejected(idx, tk, tk, "ticker is a common word "
                                                        "(still matched after an exchange prefix)"))
                else:
                    self._add(specs, AliasSpec(idx, key_of(toks), tuple(toks), "ticker", EXACT, tk))
            for pkey, ptoks in prefixes.items():
                full = list(ptoks) + toks
                self._add(specs, AliasSpec(idx, key_of(full), tuple(full), "ticker", TICKER, tk,
                                           skip_tokens=len(ptoks)))

    @staticmethod
    def _finalise(specs: dict) -> list[AliasSpec]:
        # If the same key is present with a weaker case requirement, the
        # stricter duplicates are redundant.
        by_key: dict[str, list[AliasSpec]] = {}
        for s in specs.values():
            by_key.setdefault(s.key, []).append(s)
        out: list[AliasSpec] = []
        for key, lst in by_key.items():
            weakest = min(_CASE_RANK[s.case_mode] for s in lst)
            if weakest == 0:
                keep = [s for s in lst if s.case_mode == INSENSITIVE]
                keep = [min(keep, key=lambda s: s.source != "canonical")]
            else:
                keep = lst
            out.extend(keep)
        return out

    def block_keys(self, phrases: list[str]) -> list[str]:
        return [k for k in (key_of(alias_tokens(p)) for p in phrases) if k]
