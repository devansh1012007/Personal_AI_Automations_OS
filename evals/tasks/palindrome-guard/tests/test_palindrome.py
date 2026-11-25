"""The palindrome-guard contract: normalize, then compare."""

from __future__ import annotations

import pytest
from palindrome import is_palindrome


@pytest.mark.parametrize("text", ["racecar", "noon", "a", "aa"])
def test_the_simple_palindromes(text: str) -> None:
    assert is_palindrome(text) is True


@pytest.mark.parametrize("text", ["python", "ab", "palindrome"])
def test_the_simple_non_palindromes(text: str) -> None:
    assert is_palindrome(text) is False


def test_case_is_folded() -> None:
    assert is_palindrome("RaceCar") is True


def test_punctuation_is_ignored() -> None:
    assert is_palindrome("A man, a plan, a canal: Panama") is True


def test_quotes_are_ignored() -> None:
    assert is_palindrome("No 'x' in Nixon") is True


def test_digits_count_as_characters() -> None:
    assert is_palindrome("1 2 1") is True
    assert is_palindrome("1 2 3") is False


def test_text_that_normalizes_to_nothing_is_a_palindrome() -> None:
    assert is_palindrome("") is True
    assert is_palindrome("   ") is True
    assert is_palindrome(".,:;!?") is True


def test_mixed_case_and_punctuation_together() -> None:
    assert is_palindrome("Was it a car or a cat I saw?") is True
    assert is_palindrome("This is, clearly, not one.") is False
