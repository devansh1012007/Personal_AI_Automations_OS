"""The reverse-util contract."""

from __future__ import annotations


def test_a_string_reverses_to_a_string() -> None:
    from utils.reverse import reverse

    assert reverse("abc") == "cba"
    assert isinstance(reverse("abc"), str)


def test_a_list_reverses_to_a_list() -> None:
    from utils.reverse import reverse

    assert reverse([1, 2, 3]) == [3, 2, 1]
    assert isinstance(reverse([1, 2, 3]), list)


def test_a_tuple_reverses_to_a_tuple() -> None:
    from utils.reverse import reverse

    assert reverse((1, 2)) == (2, 1)
    assert isinstance(reverse((1, 2)), tuple)


def test_empty_sequences_round_trip() -> None:
    from utils.reverse import reverse

    assert reverse("") == ""
    assert reverse([]) == []
    assert reverse(()) == ()


def test_the_input_is_never_mutated() -> None:
    from utils.reverse import reverse

    items = [1, 2, 3]
    reverse(items)
    assert items == [1, 2, 3]
