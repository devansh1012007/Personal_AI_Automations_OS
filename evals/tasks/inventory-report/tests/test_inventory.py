"""The inventory-report contract: the total, from the data as shipped."""

from __future__ import annotations

from inventory import ITEMS_CSV, load_items, total_value


def test_the_fixture_loads_three_rows() -> None:
    rows = load_items()
    assert len(rows) == 3
    assert [row["name"] for row in rows] == ["widget", "gadget", "gizmo"]


def test_the_total_value_is_the_arithmetic_answer() -> None:
    # 4 x 2.50 + 2 x 10.00 + 1 x 33.50
    assert total_value() == 63.5


def test_the_total_is_a_rounded_float() -> None:
    value = total_value()
    assert isinstance(value, float)
    assert round(value, 2) == value


def test_the_data_file_is_still_the_fixture() -> None:
    text = ITEMS_CSV.read_text(encoding="utf-8")
    assert "widget,4,2.50" in text
    assert "gizmo,1,33.50" in text
