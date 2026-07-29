"""Keyword normalization and matching logic.

Facebook Marketplace's own search is fuzzy about punctuation, so we send a
loose query to the API and do precise matching ourselves against the title
(and description, if fetched) rather than trusting the query parameter alone.
"""
from __future__ import annotations

import re


def normalize(text: str) -> str:
    """Lowercase and strip everything except letters and digits.

    '285/70R18', '285 70 18', and 'LT285/70R18' all normalize to comparable
    forms, so a single stored keyword variant can match several real-world
    spellings sellers actually use.
    """
    return re.sub(r"[^a-z0-9]", "", text.lower())


def to_broad_query(keyword: str) -> str:
    """Turn a punctuation-heavy keyword into a plain-text search query."""
    cleaned = "".join(ch if (ch.isalnum() or ch == " ") else " " for ch in keyword)
    return " ".join(cleaned.split())


def matches_any_keyword(text: str, keywords: list[str]) -> bool:
    norm_text = normalize(text)
    return any(normalize(kw) in norm_text for kw in keywords if kw)


def matches_exclusions(text: str, exclude_keywords: list[str]) -> bool:
    """True if the text hits a negative keyword and should be filtered out."""
    if not exclude_keywords:
        return False
    norm_text = normalize(text)
    return any(normalize(kw) in norm_text for kw in exclude_keywords if kw)
