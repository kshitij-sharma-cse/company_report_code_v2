"""company_finder - find mentions of known companies in documents.

Phase 1: deterministic alias + Aho-Corasick matching (optional fuzzy) plus
definition→scoped alias. Optional Phase 2: scoped OpenRouter LLM (--llm, off by default).
"""
from .config import Config
from .loader import Company, load_companies
from .match import CompanyMatcher
from .mention import Mention, Passage, dedupe_identical, dedupe_output, dedupe_passages
from .matchers import BaseMatcher, MatchContext

__version__ = "1.4.6"
__all__ = ["Config", "Company", "load_companies", "CompanyMatcher", "Mention",
           "Passage", "dedupe_identical", "dedupe_output", "dedupe_passages",
           "BaseMatcher", "MatchContext", "__version__"]
