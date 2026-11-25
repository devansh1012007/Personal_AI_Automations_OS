"""Decide whether text reads the same both ways."""

from __future__ import annotations


def is_palindrome(text: str) -> bool:
    """Whether ``text`` is a palindrome, after normalization.

    Normalization folds case and drops every non-alphanumeric
    character, so punctuation, spacing and casing never decide the
    answer. Text that normalizes to nothing counts as a palindrome.
    """
    cleaned = [ch.casefold() for ch in text if ch.isalnum()]
    return cleaned == cleaned[::-1]
