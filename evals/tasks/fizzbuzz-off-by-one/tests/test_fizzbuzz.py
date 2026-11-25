"""The fizzbuzz-off-by-one contract: 1..n inclusive, never 0."""

from __future__ import annotations

import pytest
from fizzbuzz import fizzbuzz


def test_five_lines_for_five() -> None:
    assert fizzbuzz(5) == ["1", "2", "Fizz", "4", "Buzz"]


def test_fifteen_ends_on_fizzbuzz() -> None:
    lines = fizzbuzz(15)
    assert lines[-1] == "FizzBuzz"
    assert len(lines) == 15


def test_zero_never_appears() -> None:
    for n in (1, 3, 15, 100):
        assert "0" not in fizzbuzz(n)


def test_n_is_included() -> None:
    assert fizzbuzz(4)[-1] == "4"
    assert fizzbuzz(6)[-1] == "Fizz"


def test_zero_asked_for_zero_given() -> None:
    assert fizzbuzz(0) == []


def test_one_asked_for_one_line() -> None:
    assert fizzbuzz(1) == ["1"]


@pytest.mark.parametrize(
    ("n", "word"),
    [(3, "Fizz"), (5, "Buzz"), (9, "Fizz"), (10, "Buzz"), (30, "FizzBuzz")],
)
def test_the_classic_words(n: int, word: str) -> None:
    assert fizzbuzz(n)[n - 1] == word
