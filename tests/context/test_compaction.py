"""Tests for deterministic, non-LLM context compaction.

Two properties carry the design and both are asserted over hypothesis-generated
text as well as worked examples:

* **idempotence** — `compact(compact(x)) == compact(x)`
* **non-expansion** — `len(compact(x)) <= len(x)`

A compactor that can grow its input would be a denial-of-service vector on the
one path that exists to prevent one, so the second property is checked on every
public entry point rather than only on `compact`.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from paa.config import ContextSettings
from paa.context.budget import CharEstimator
from paa.context.compaction import (
    DEFAULT_ELLIPSIS,
    DEFAULT_MAX_LINE_CHARS,
    ContextCompactor,
)


@pytest.fixture
def compactor() -> ContextCompactor:
    return ContextCompactor(estimator=CharEstimator(4.0))


# Arbitrary text, plus markdown-flavoured text that actually exercises the
# decoration rules. Random unicode alone almost never produces a `**bold**`.
_FRAGMENTS = st.sampled_from(
    [
        "# heading",
        "### deeper heading",
        "**bold text**",
        "*italic*",
        "***nested emphasis***",
        "`inline code`",
        "```python",
        "```",
        "> a quotation",
        ">> nested quotation",
        "- a bullet",
        "* another bullet",
        "+ third bullet",
        "---",
        "___",
        "* * *",
        "[link text](https://example.invalid/x)",
        "![alt text](https://example.invalid/i.png)",
        "plain sentence",
        "duplicated line",
        "duplicated line",
        "   lots    of     spaces   ",
        "\ttab\tseparated\tvalues",
        "",
        "",
        "x" * 400,
        "## # awkward heading",
        "key: " + "v" * 500,
    ]
)
_markdown = st.lists(_FRAGMENTS, max_size=30).map("\n".join)
_any_text = st.text(max_size=400) | _markdown


# ---------------------------------------------------------------------------
# Whitespace
# ---------------------------------------------------------------------------


def test_runs_of_horizontal_whitespace_collapse(compactor: ContextCompactor) -> None:
    assert compactor.compact("a    b\t\t\tc") == "a b c"


def test_leading_and_trailing_whitespace_is_stripped(compactor: ContextCompactor) -> None:
    assert compactor.compact("   padded line   ") == "padded line"


def test_runs_of_blank_lines_collapse_to_one(compactor: ContextCompactor) -> None:
    """Paragraph structure survives; only the excess separators go."""
    assert compactor.compact("a\n\n\n\n\nb") == "a\n\nb"


def test_leading_and_trailing_blank_lines_are_removed(compactor: ContextCompactor) -> None:
    assert compactor.compact("\n\n\nbody\n\n\n") == "body"


def test_line_endings_are_normalised(compactor: ContextCompactor) -> None:
    assert compactor.compact("a\r\nb\rc") == "a\nb\nc"


def test_whitespace_only_input_compacts_to_empty(compactor: ContextCompactor) -> None:
    assert compactor.compact("   \n\t\n  \r\n ") == ""


def test_empty_input_stays_empty(compactor: ContextCompactor) -> None:
    assert compactor.compact("") == ""


# ---------------------------------------------------------------------------
# Markdown decoration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("# Heading", "Heading"),
        ("###### Deep", "Deep"),
        ("## # awkward", "awkward"),  # needs two passes; the fixed point handles it
        ("> quoted", "quoted"),
        (">> double quoted", "double quoted"),
        ("- bullet", "bullet"),
        ("* bullet", "bullet"),
        ("+ bullet", "bullet"),
        ("**bold**", "bold"),
        ("__bold__", "bold"),
        ("*italic*", "italic"),
        ("***both***", "both"),
        ("`code`", "code"),
        ("``code``", "code"),
        ("text with **bold** inside", "text with bold inside"),
        ("[link text](https://example.invalid)", "link text"),
        ("![alt](https://example.invalid/i.png)", "alt"),
        ("---", ""),
        ("___", ""),
        ("* * *", ""),
        ("```python", ""),
        ("```", ""),
    ],
)
def test_markdown_decoration_is_stripped(
    compactor: ContextCompactor, raw: str, expected: str
) -> None:
    assert compactor.compact(raw) == expected


def test_snake_case_identifiers_survive(compactor: ContextCompactor) -> None:
    """Underscore-italic stripping would mangle identifiers, so it is not done."""
    assert compactor.compact("call site_id_value now") == "call site_id_value now"


def test_arithmetic_asterisks_survive(compactor: ContextCompactor) -> None:
    assert compactor.compact("compute 2 * 3 * 4 here") == "compute 2 * 3 * 4 here"


def test_content_words_are_never_dropped(compactor: ContextCompactor) -> None:
    result = compactor.compact("## **Important:** the `token_ceiling` is *1500*")
    assert result == "Important: the token_ceiling is 1500"


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def test_duplicate_lines_are_dropped_keeping_the_first(compactor: ContextCompactor) -> None:
    assert compactor.compact("alpha\nbeta\nalpha\ngamma\nbeta") == "alpha\nbeta\ngamma"


def test_lines_differing_only_in_whitespace_dedupe_together(
    compactor: ContextCompactor,
) -> None:
    assert compactor.compact("value\n  value  \nvalue\t") == "value"


def test_lines_differing_only_in_decoration_dedupe_together(
    compactor: ContextCompactor,
) -> None:
    assert compactor.compact("- item\n* item\n**item**") == "item"


def test_blank_lines_are_exempt_from_deduplication(compactor: ContextCompactor) -> None:
    """Deduping blanks would keep only the first and weld every paragraph together."""
    assert compactor.compact("a\n\nb\n\nc") == "a\n\nb\n\nc"


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


def test_long_lines_are_truncated_with_a_marker() -> None:
    compactor = ContextCompactor(estimator=CharEstimator(4.0), max_line_chars=20)
    result = compactor.compact("x" * 100)
    assert len(result) == 20
    assert result.endswith(DEFAULT_ELLIPSIS)
    assert result == "x" * 17 + "..."


def test_lines_at_the_limit_are_left_alone() -> None:
    compactor = ContextCompactor(estimator=CharEstimator(4.0), max_line_chars=20)
    assert compactor.compact("y" * 20) == "y" * 20


def test_truncation_is_idempotent() -> None:
    compactor = ContextCompactor(estimator=CharEstimator(4.0), max_line_chars=20)
    once = compactor.compact("z" * 500)
    assert compactor.compact(once) == once


def test_default_line_cap_is_applied(compactor: ContextCompactor) -> None:
    assert len(compactor.compact("q" * 5000)) == DEFAULT_MAX_LINE_CHARS


def test_a_custom_marker_is_used() -> None:
    compactor = ContextCompactor(
        estimator=CharEstimator(4.0), max_line_chars=12, ellipsis="[cut]"
    )
    result = compactor.compact("w" * 60)
    assert result == "wwwwwww[cut]"


# ---------------------------------------------------------------------------
# Idempotence and non-expansion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "plain",
        "# H\n\n\n**b**   c\n**b**   c\n> q\n- i\n",
        "a\r\n\r\nb\r\n",
        "***nested***\n***nested***",
        "```py\ncode\n```",
        "## # ## awkward",
        "x" * 1000,
        "  \n \t \n  ",
        "[a](b) [a](b)",
    ],
)
def test_compaction_is_idempotent_on_examples(compactor: ContextCompactor, raw: str) -> None:
    once = compactor.compact(raw)
    assert compactor.compact(once) == once


@given(_any_text)
def test_compaction_is_idempotent(text: str) -> None:
    """Guaranteed structurally: `compact` runs the reduction to a fixed point."""
    compactor = ContextCompactor(estimator=CharEstimator(4.0))
    once = compactor.compact(text)
    assert compactor.compact(once) == once


@given(_any_text)
def test_compaction_never_grows_its_input(text: str) -> None:
    compactor = ContextCompactor(estimator=CharEstimator(4.0))
    assert len(compactor.compact(text)) <= len(text)


@given(_any_text, st.integers(min_value=1, max_value=80))
def test_compaction_never_grows_at_any_line_cap(text: str, cap: int) -> None:
    compactor = ContextCompactor(estimator=CharEstimator(4.0), max_line_chars=cap + 3)
    result = compactor.compact(text)
    assert len(result) <= len(text)
    assert compactor.compact(result) == result


@given(_any_text)
def test_compaction_never_increases_the_token_estimate(text: str) -> None:
    compactor = ContextCompactor(estimator=CharEstimator(4.0))
    assert compactor.estimator.estimate(compactor.compact(text)) <= compactor.estimator.estimate(
        text
    )


# ---------------------------------------------------------------------------
# compact_to_budget
# ---------------------------------------------------------------------------


def test_text_already_within_budget_is_only_compacted(compactor: ContextCompactor) -> None:
    result = compactor.compact_to_budget("# Title\n\n\nbody   text", 100)
    assert result == "Title\n\nbody text"


def test_over_budget_text_drops_trailing_lines_first(compactor: ContextCompactor) -> None:
    """Lines carry meaning as units, so whole-line removal beats a mid-sentence cut."""
    text = "\n".join(f"line number {i}" for i in range(20))
    result = compactor.compact_to_budget(text, 10)
    assert compactor.fits(result, 10)
    assert result.startswith("line number 0")
    assert "line number 19" not in result
    # A clean prefix of whole lines, not a character-level cut.
    assert all(line.startswith("line number ") for line in result.split("\n"))


def test_a_single_over_budget_line_is_character_truncated(
    compactor: ContextCompactor,
) -> None:
    result = compactor.compact_to_budget("a" * 200, 5)
    assert compactor.fits(result, 5)
    assert len(result) <= 20
    assert result.endswith(DEFAULT_ELLIPSIS)


def test_a_zero_token_budget_yields_empty_text(compactor: ContextCompactor) -> None:
    assert compactor.compact_to_budget("some fairly long content here", 0) == ""


def test_budget_too_small_for_the_marker_drops_the_marker() -> None:
    """With one token to spend, spending three characters on "..." is not affordable."""
    compactor = ContextCompactor(estimator=CharEstimator(1.0))
    result = compactor.compact_to_budget("abcdefghij", 2)
    assert compactor.fits(result, 2)
    assert len(result) <= 2


def test_compact_to_budget_uses_the_largest_prefix_that_fits() -> None:
    compactor = ContextCompactor(estimator=CharEstimator(1.0))
    result = compactor.compact_to_budget("abcdefghijklmnop", 8)
    assert len(result) == 8  # 5 content chars + the 3-char marker
    assert result == "abcde..."


def test_fits_reports_the_estimator_verdict(compactor: ContextCompactor) -> None:
    assert compactor.fits("abcd", 1) is True
    assert compactor.fits("abcde", 1) is False


@given(_any_text, st.integers(min_value=0, max_value=150))
def test_compact_to_budget_always_fits_and_never_grows(text: str, max_tokens: int) -> None:
    """The guarantee the ceiling depends on, over arbitrary input."""
    compactor = ContextCompactor(estimator=CharEstimator(4.0))
    result = compactor.compact_to_budget(text, max_tokens)
    assert compactor.estimator.estimate(result) <= max_tokens
    assert len(result) <= len(text)


@given(_any_text, st.integers(min_value=0, max_value=60))
def test_compact_to_budget_fits_under_a_strict_estimator(text: str, max_tokens: int) -> None:
    """One character per token: the tightest possible budget pressure."""
    compactor = ContextCompactor(estimator=CharEstimator(1.0))
    result = compactor.compact_to_budget(text, max_tokens)
    assert len(result) <= max_tokens
    assert len(result) <= len(text)


# ---------------------------------------------------------------------------
# Construction and validation
# ---------------------------------------------------------------------------


def test_default_estimator_comes_from_settings() -> None:
    compactor = ContextCompactor(settings=ContextSettings(chars_per_token=2.0))
    assert compactor.estimator.estimate("abcd") == 2


@pytest.mark.parametrize("cap", [0, 1, 3])
def test_line_cap_must_leave_room_for_the_marker(cap: int) -> None:
    with pytest.raises(ValueError, match="must exceed len"):
        ContextCompactor(max_line_chars=cap, ellipsis="...")


def test_a_line_cap_one_over_the_marker_is_accepted() -> None:
    compactor = ContextCompactor(estimator=CharEstimator(4.0), max_line_chars=4)
    assert compactor.compact("abcdefgh") == "a..."


@pytest.mark.parametrize("bad", [1.5, "20", True])
def test_line_cap_must_be_an_int(bad: object) -> None:
    with pytest.raises(ValueError, match="max_line_chars must be an int"):
        ContextCompactor(max_line_chars=bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [-1, 1.5, "10", True])
def test_budget_must_be_a_non_negative_int(compactor: ContextCompactor, bad: object) -> None:
    with pytest.raises(ValueError, match="max_tokens"):
        compactor.compact_to_budget("text", bad)  # type: ignore[arg-type]


def test_repr_is_informative(compactor: ContextCompactor) -> None:
    assert "max_line_chars=" in repr(compactor)
