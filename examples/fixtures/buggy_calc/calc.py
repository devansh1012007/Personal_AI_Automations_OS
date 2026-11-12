"""A tiny calculator — the S1 demo target (``examples/fixtures/buggy_calc``).

The one bug this fixture carries, on purpose, is in :func:`divide`:
it promises "return ``a`` divided by ``b`` as a float" and then
does floor division, so every fractional result is wrong while
every exact one is right. ``test_calc.py::test_divide_returns_fraction``
is the test that notices. Nothing else here is wrong.

This file lives outside the repo's pytest suite on purpose: the
repository's ``pyproject.toml`` sets ``testpaths = ["tests"]``, so
this failing test can never make the main suite red. It is a
target for ``paa agent --workspace``, not a test of this repo.
"""


def add(a: int, b: int) -> int:
    """Return the sum of two integers."""
    return a + b


def subtract(a: int, b: int) -> int:
    """Return ``a`` minus ``b``."""
    return a - b


def multiply(a: int, b: int) -> int:
    """Return the product of two integers."""
    return a * b


def divide(a: int, b: int) -> float:
    """Return ``a`` divided by ``b`` as a float."""
    if b == 0:
        raise ValueError("division by zero")
    return a // b
