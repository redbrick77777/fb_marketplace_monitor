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


def _words(text: str) -> list[str]:
    """Lowercase alphanumeric words, with digit-grouping commas/dots removed
    first so '8,900 miles' is the single word '8900' rather than '8' + '900'.
    """
    text = re.sub(r"(?<=\d)[,.](?=\d)", "", text.lower())
    return re.findall(r"[a-z0-9]+", text)


def _word_set(text: str) -> set[str]:
    """Every word in the text, plus each word's letter/digit runs, so '890ADV'
    also yields '890' and 'adv', and 'R1250GS' yields 'r', '1250' and 'gs'."""
    words: set[str] = set()
    for word in _words(text):
        words.add(word)
        words.update(re.findall(r"[a-z]+|[0-9]+", word))
    return words


def matches_any_keyword(text: str, keywords: list[str], any_order: bool = False) -> bool:
    """True if the text contains any keyword.

    Default rule: the keyword's characters appear contiguously, ignoring case,
    spaces and punctuation ('R1250 GS' matches 'r1250gs'), so word order
    matters - 'KTM 890' does NOT match '890 KTM Adventure R'.

    any_order=True additionally accepts the text when every word of the
    keyword appears in it as a whole word, in any order. It only ever adds
    matches. Whole words, not substrings, so a short keyword like '890' can't
    hit '8,900 miles' or a phone number buried in a description.
    """
    norm_text = normalize(text)
    if any(normalize(kw) in norm_text for kw in keywords if kw):
        return True
    if not any_order:
        return False
    text_words = _word_set(text)
    for kw in keywords:
        kw_words = _words(kw)
        if kw_words and all(w in text_words for w in kw_words):
            return True
    return False


def matches_exclusions(text: str, exclude_keywords: list[str]) -> bool:
    """True if the text hits a negative keyword and should be filtered out."""
    if not exclude_keywords:
        return False
    norm_text = normalize(text)
    return any(normalize(kw) in norm_text for kw in exclude_keywords if kw)
