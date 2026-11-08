"""Matching configuration.

Nothing company-specific lives here: these are generic linguistic defaults
(legal suffixes, generic stop words, case policies).  Every field can be
overridden with a JSON file passed via ``--config`` / ``Config.from_file``.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent / "data"

# Generic legal-form suffixes.  Stripped from the END of names to create the
# "core" variant (e.g. "Infosys Limited" -> "Infosys").  Compared on
# normalised lower-case tokens, so "Ltd." / "LTD" / "ltd" are all "ltd".
DEFAULT_LEGAL_SUFFIXES = [
    "ltd", "limited", "pvt", "private", "inc", "incorporated", "corp",
    "corporation", "llc", "llp", "lp", "plc", "ag", "sa", "nv", "bv", "gmbh",
    "co", "company", "se", "spa", "srl", "sarl", "pte", "pty", "bhd", "kk",
    "ab", "asa", "oyj", "as", "kgaa", "na", "pllc", "lllp", "pbc", "fsb", "ulc",
]

# Suffixes stripped only when at least two meaningful tokens remain
# ("Goldman Sachs Group" -> "Goldman Sachs", but "Alphabet Holdings" is kept).
DEFAULT_SECONDARY_SUFFIXES = ["holdings", "holding", "group"]

# Legal suffixes that may follow a matched name in the text and get absorbed
# into the reported span ("Reliance Industries Ltd." -> one mention).
DEFAULT_EXTENSION_SUFFIXES = [
    "ltd", "limited", "pvt", "private", "inc", "incorporated", "corp",
    "corporation", "llc", "llp", "plc", "ag", "sa", "nv", "bv", "gmbh", "se",
    "spa", "pte", "pty", "bhd", "lp", "na", "pllc", "lllp", "pbc", "co",
]

# Stock-exchange prefixes: a ticker right after one of these ("(NYSE: C)",
# "Nasdaq: AAPL") is unambiguous, so even 1-2 letter tickers are matched there.
DEFAULT_EXCHANGE_PREFIXES = [
    "NYSE", "NASDAQ", "NYSE American", "NYSE Arca", "NYSE MKT", "NasdaqGS",
    "NasdaqGM", "NasdaqCM", "OTC", "OTCQX", "OTCQB", "OTC Pink", "Cboe", "BATS",
    "TSX", "LSE", "NSE", "BSE",
]

# Words that, right after a matched name, indicate a *different* but related
# legal entity ("Acme Buyer Holdings, LLC", "Acme Intermediate Holdings II").
# Such mentions are kept but flagged with a note and a lower score.
DEFAULT_ENTITY_WORDS = [
    "holdings", "holding", "intermediate", "parent", "topco", "midco", "holdco",
    "bidco", "opco", "newco", "buyer", "acquisition", "acquisitions", "acquiror",
    "acquirer", "borrower", "merger", "sub", "blocker", "aggregator", "feeder",
    "group",
]

# Generic words that must never be an alias on their own.  An alias made only
# of these words (plus connectors / legal suffixes) is rejected, except when it
# is a company's full core name or an explicit multi-word alias (logged).
DEFAULT_STOP_WORDS = [
    "capital", "holdings", "holding", "group", "growth", "first", "bank",
    "banks", "banking", "finance", "financial", "finserv", "industries",
    "industry", "global", "india", "indian", "international", "investment",
    "investments", "securities", "services", "service", "solutions",
    "technologies", "technology", "tech", "systems", "enterprises",
    "enterprise", "ventures", "partners", "fund", "funds", "trust", "asset",
    "assets", "management", "insurance", "life", "general", "national",
    "united", "american", "new", "energy", "power", "infrastructure",
    "resources", "corporate", "consultancy", "consulting", "motors", "steel",
    "cement", "chemicals", "pharma", "pharmaceuticals", "retail", "digital",
    "media", "communications", "telecom", "realty", "properties", "estate",
    "equity", "credit", "markets", "market", "exchange", "reserve", "central",
    "state", "public", "royal", "standard", "commercial", "merchant", "mutual",
    "wealth", "advisors", "advisory", "research", "value", "prime", "premier",
    "alpha", "beta", "north", "south", "east", "west", "asia", "europe",
    "america", "world", "universal", "ltd", "limited", "inc", "company",
]

# Connector words ignored when judging whether an alias is "all generic" and
# allowed to be lower-case inside capitalised names ("Bank of India").
DEFAULT_CONNECTORS = ["of", "and", "the", "for", "de", "la", "du", "von", "van"]


@dataclass
class Config:
    legal_suffixes: list[str] = field(default_factory=lambda: list(DEFAULT_LEGAL_SUFFIXES))
    secondary_suffixes: list[str] = field(default_factory=lambda: list(DEFAULT_SECONDARY_SUFFIXES))
    extension_suffixes: list[str] = field(default_factory=lambda: list(DEFAULT_EXTENSION_SUFFIXES))
    stop_words: list[str] = field(default_factory=lambda: list(DEFAULT_STOP_WORDS))
    connectors: list[str] = field(default_factory=lambda: list(DEFAULT_CONNECTORS))
    extra_stop_words: list[str] = field(default_factory=list)

    # Minimum alias length (letters/digits, spaces excluded).
    min_alias_length: int = 3
    # Explicit case-sensitive aliases / tickers may be shorter (e.g. "3M").
    min_case_sensitive_alias_length: int = 2

    # Single-word aliases found in the common-words list must match with a
    # capital first letter.  Policy: "common_words" | "all" | "none".
    single_word_case_policy: str = "common_words"
    common_words_file: str = str(DATA_DIR / "common_words.txt")
    extra_common_words: list[str] = field(default_factory=list)
    # Treat all-caps aliases/tickers up to this many letters as case-sensitive
    # even if not listed in case_sensitive_aliases (0 disables).
    auto_case_sensitive_acronym_max_len: int = 5
    # Score given to an ambiguous common-word alias matched at a sentence start
    # or in an ALL-CAPS run (where capitalisation carries no signal).
    ambiguous_weak_context_score: float = 60.0

    # Multi-word aliases made only of common English words ("Home Depot",
    # "General Motors", "Capital One") need every word capitalised:
    # "capitalised" (default) | "none".
    multi_word_common_case_policy: str = "capitalised"
    # A capitalised ambiguous single word followed by another capitalised common
    # word ("Our Target Market") is scored like a sentence start.
    title_case_weak_context: bool = True

    # Tickers: free-standing (exact case) only if at least this long and not a
    # common word; always matched right after an exchange prefix.
    free_ticker_min_length: int = 3
    exchange_prefixes: list[str] = field(default_factory=lambda: list(DEFAULT_EXCHANGE_PREFIXES))

    # Similar-name handling (private companies): flag "<name> Holdings II, LLC".
    entity_words: list[str] = field(default_factory=lambda: list(DEFAULT_ENTITY_WORDS))
    longer_entity_score: float = 85.0
    # pick up document-local short names: Foo Bar, LLC ("Foo") -> later "Foo" matches
    defined_terms: bool = True

    # Global phrases that suppress any company match fully inside them
    # (e.g. "Reserve Bank of India" so it never yields "Bank of India").
    block_phrases: list[str] = field(default_factory=list)

    # Generate "JP Morgan" from "J.P. Morgan", "JP Morgan" from "JPMorgan" etc.
    join_initials: bool = True
    split_camel_acronyms: bool = True
    # Absorb trailing legal suffixes in the text into the mention span.
    extend_legal_suffixes: bool = True

    # Fuzzy pass (off by default).
    fuzzy: bool = False
    fuzzy_threshold: float = 90.0
    fuzzy_min_length: int = 5
    fuzzy_max_ngram: int = 6

    # Optional OpenRouter pass for company references that are not names
    # (for example a filing-defined "the Company" or an unambiguous pronoun).
    # Off by default — no network calls unless enabled via --llm / config.
    llm_references: bool = False
    llm_model: str = "openai/gpt-4.1"
    llm_chunk_chars: int = 10000
    llm_chunk_overlap: int = 400
    llm_min_confidence: float = 0.80
    llm_max_candidates: int = 30
    llm_timeout_seconds: int = 90
    # hits_and_tail (default) | full. full is explicit whole-document LLM.
    llm_scope: str = "hits_and_tail"
    llm_tail_pages: int = 2
    # Warn on stderr at/above this many pages or requests. Never silently drop.
    llm_warn_pages: int = 25
    # Free definition → scoped alias pass (Sponsor / non-filer Company).
    definition_aliases: bool = True
    # Filer "the Company" definition hits are suppressed unless this is set.
    include_filer_self_ref_definitions: bool = False

    # Common-word disambiguation pass (off by default -- no network calls
    # unless enabled via --llm-disambiguate / config). Checks every matched
    # mention (using the company's `description` when one is on file, name
    # + excerpt alone otherwise); decides whether the match is genuinely
    # that company or an ordinary use of the same word/substring. Calls are
    # prompt-batched (see disambiguate.py).
    llm_disambiguate_common_words: bool = False
    llm_disambiguate_timeout_seconds: int = 90
    # Prompt-batching: this many candidate mentions packed into one call
    # (one JSON array back), not a provider-side async Batch API.
    llm_disambiguate_batch_size: int = 6
    # Concurrent disambiguation calls (threads) and retries per call on a
    # transient provider error (HTTP 429/5xx, timeout, malformed output).
    llm_disambiguate_workers: int = 6
    llm_disambiguate_retries: int = 4
    # Client-side pacing: max requests per minute across all threads (0 = off).
    # OpenRouter caps new accounts at 20 RPM per model; stay just under it.
    llm_disambiguate_max_rpm: int = 18
    # A "yes" below this confidence is treated as "no" (dropped) -- see
    # disambiguate.py's module docstring for why this is a supplementary
    # safety net, not the primary defense.
    llm_disambiguate_min_confidence: int = 50

    # Snippet
    context_max_chars: int = 300

    # ------------------------------------------------------------------ io
    @classmethod
    def from_file(cls, path: str | Path | None) -> "Config":
        if not path:
            return cls()
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"Unknown config keys: {sorted(unknown)}")
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def fingerprint(self) -> str:
        """Hash of everything that influences the automaton (for caching)."""
        d = self.to_dict()
        # fuzzy / context settings don't change the automaton
        for k in ("fuzzy", "fuzzy_threshold", "context_max_chars", "common_words_file",
                  "llm_references", "llm_model", "llm_chunk_chars", "llm_chunk_overlap",
                  "llm_min_confidence", "llm_max_candidates", "llm_timeout_seconds",
                  "llm_scope", "llm_tail_pages", "llm_warn_pages",
                  "definition_aliases", "include_filer_self_ref_definitions",
                  "llm_disambiguate_common_words", "llm_disambiguate_timeout_seconds",
                  "llm_disambiguate_batch_size", "llm_disambiguate_min_confidence",
                  "llm_disambiguate_workers", "llm_disambiguate_retries",
                  "llm_disambiguate_max_rpm"):
            d.pop(k, None)
        blob = json.dumps(d, sort_keys=True).encode()
        cw = Path(self.common_words_file)
        if cw.exists():
            blob += cw.read_bytes()
        return hashlib.sha256(blob).hexdigest()

    # --------------------------------------------------------- derived sets
    def stop_set(self) -> set[str]:
        return {w.lower() for w in (*self.stop_words, *self.extra_stop_words)}

    def common_words(self) -> set[str]:
        words: set[str] = set()
        p = Path(self.common_words_file)
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip().lower()
                if line and not line.startswith("#"):
                    words.add(line)
        words.update(w.lower() for w in self.extra_common_words)
        return words
