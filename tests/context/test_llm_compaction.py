"""Tests for the optional LLM summarizer seam on ``ContextCompactor``.

The compactor is mechanical and deterministic by default; an optional injected
``summarizer`` callable (``str -> str``) may further compress text after the
mechanical fixed point. This file proves both paths:

* with a summarizer: it is called exactly once per ``compact()`` on the
  mechanically-reduced text, and its output is returned;
* without a summarizer (default): behavior is unchanged — purely mechanical,
  deterministic, idempotent.

A call-counting summarizer is used so the "exactly once" and
"used-when-injected / not-used-when-absent" claims are asserted structurally.
"""

from __future__ import annotations

import pytest

from paa.context.compaction import ContextCompactor


class _RecordingSummarizer:
    """A ``str -> str`` summarizer that records every input it receives."""

    def __init__(self, output: str = "SUMMARY") -> None:
        self.output = output
        self.calls: list[str] = []

    def __call__(self, text: str) -> str:
        self.calls.append(text)
        return self.output


@pytest.fixture
def summarizer() -> _RecordingSummarizer:
    return _RecordingSummarizer()


# ---------------------------------------------------------------------------
# Injected summarizer path
# ---------------------------------------------------------------------------


def test_compact_returns_summarizer_output(summarizer: _RecordingSummarizer) -> None:
    compactor = ContextCompactor(summarizer=summarizer)
    result = compactor.compact("any input text at all")
    assert result == "SUMMARY"


def test_summarizer_is_called_exactly_once_per_compact(
    summarizer: _RecordingSummarizer,
) -> None:
    compactor = ContextCompactor(summarizer=summarizer)
    compactor.compact("first call")
    compactor.compact("second call")
    assert summarizer.calls == ["first call", "second call"]
    assert len(summarizer.calls) == 2


def test_summarizer_receives_the_mechanical_fixed_point(
    summarizer: _RecordingSummarizer,
) -> None:
    """The summarizer gets the mechanically-reduced text, not the raw input."""
    compactor = ContextCompactor(summarizer=summarizer)
    compactor.compact("**bold**   line\n\n**bold**   line\n   extra   spaces   ")
    assert summarizer.calls == ["bold line\n\nextra spaces"]


def test_summarizer_is_invoked_once_on_empty_input(
    summarizer: _RecordingSummarizer,
) -> None:
    compactor = ContextCompactor(summarizer=summarizer)
    assert compactor.compact("") == "SUMMARY"
    assert summarizer.calls == [""]


def test_summarizer_output_passes_through_even_when_longer(
    summarizer: _RecordingSummarizer,
) -> None:
    """With a summarizer, non-expansion is waived: longer output passes through."""
    summarizer.output = "x" * 500
    compactor = ContextCompactor(summarizer=summarizer)
    result = compactor.compact("short")
    assert result == "x" * 500


# ---------------------------------------------------------------------------
# Mechanical default (summarizer is None)
# ---------------------------------------------------------------------------


def test_default_is_purely_mechanical_and_idempotent() -> None:
    """No summarizer: unchanged behavior, idempotent mechanical output."""
    compactor = ContextCompactor()
    raw = "# Heading\n\n\n**bold**   text\n**bold**   text\n> quote\n- item\n"
    once = compactor.compact(raw)
    assert compactor.compact(once) == once
    assert once == "Heading\n\nbold text\nquote\nitem"


def test_no_summarizer_means_no_model_like_call() -> None:
    """Default construction stores None and never invokes a summarizer."""
    compactor = ContextCompactor()
    assert compactor.summarizer is None
    assert compactor.compact("still mechanical") == "still mechanical"


# ---------------------------------------------------------------------------
# Construction validation is unchanged
# ---------------------------------------------------------------------------


def test_summarizer_parameter_is_keyword_only() -> None:
    with pytest.raises(TypeError):
        ContextCompactor(lambda x: x)  # type: ignore[call-arg]


def test_repr_unchanged_when_summarizer_absent() -> None:
    compactor = ContextCompactor()
    assert "max_line_chars=" in repr(compactor)
