"""Pluggable matchers.

Every matcher implements :class:`BaseMatcher.find`, receives a
:class:`MatchContext` (original text, normalised text, mentions found so far
and a per-token coverage map) and returns new :class:`Mention` objects.
:class:`~company_finder.match.CompanyMatcher` runs them in order:

1. ``ExactMatcher``  - Aho-Corasick over all generated aliases (always on)
2. ``FuzzyMatcher``  - rapidfuzz over capitalised n-grams (``--fuzzy``)
3. any extra matchers registered with ``CompanyMatcher.add_matcher`` - future
   extension point (e.g. a redesigned Phase-2 matcher over
   ``ctx.unmatched_passages()``).
"""
from .base import BaseMatcher, MatchContext
from .exact import ExactMatcher
from .fuzzy import FuzzyMatcher

__all__ = [
    "BaseMatcher", "MatchContext", "ExactMatcher", "FuzzyMatcher",
]
