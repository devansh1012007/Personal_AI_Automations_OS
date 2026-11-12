"""Tests for the S1 calculator fixture.

Exactly one test fails against the shipped ``calc.py``:
``test_divide_returns_fraction``. ``divide(1, 2)`` must be ``0.5``,
and the shipped implementation floors it to ``0``. Every other
test passes, so a fix that breaks a correct function is visible
immediately. Run from this directory:

    python -m pytest test_calc.py -q
"""

import pytest
from calc import add, divide, multiply, subtract


def test_add() -> None:
    assert add(2, 3) == 5
    assert add(-1, 1) == 0


def test_subtract() -> None:
    assert subtract(2, 3) == -1
    assert subtract(5, 5) == 0


def test_multiply() -> None:
    assert multiply(2, 3) == 6
    assert multiply(-2, 3) == -6


def test_divide_returns_fraction() -> None:
    assert divide(1, 2) == 0.5
    assert divide(7, 2) == 3.5


def test_divide_by_zero_raises() -> None:
    with pytest.raises(ValueError):
        divide(1, 0)
