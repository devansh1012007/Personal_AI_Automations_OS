"""A tiny inventory: items, quantities, unit prices."""

from __future__ import annotations

import csv
from pathlib import Path

ITEMS_CSV = Path(__file__).parent / "items.csv"


def load_items() -> list[dict[str, str]]:
    """Every inventory row, as the CSV names it."""
    with ITEMS_CSV.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def total_value() -> float:
    """The inventory's total value: sum of qty x unit price, 2 decimals.

    Reading the data is `load_items`'s business; this function only
    does the arithmetic.
    """
    total = sum(
        int(row["qty"]) * float(row["unit_price"]) for row in load_items()
    )
    return round(total, 2)
