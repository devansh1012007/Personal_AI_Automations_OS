"""FizzBuzz as a list of lines, one per number from 1 to n inclusive."""

from __future__ import annotations


def fizzbuzz(n: int) -> list[str]:
    """Return the FizzBuzz lines for ``1..n`` inclusive.

    Multiples of 3 read "Fizz", of 5 "Buzz", of both "FizzBuzz",
    everything else its own decimal spelling.
    """
    lines: list[str] = []
    for i in range(n):
        if i % 15 == 0:
            lines.append("FizzBuzz")
        elif i % 3 == 0:
            lines.append("Fizz")
        elif i % 5 == 0:
            lines.append("Buzz")
        else:
            lines.append(str(i))
    return lines
