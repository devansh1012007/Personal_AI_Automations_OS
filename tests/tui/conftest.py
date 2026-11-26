"""Collection guard for the chat TUI suite.

The modules under tests/tui/ import ``paa.tui.chat.app`` (and the runner /
dashboard panels), which import Textual at module scope. Textual lives in the
``tui`` extra (``pip install -e ".[tui]"``); on an install without it the
import raises ``ModuleNotFoundError`` during COLLECTION, which is worse than a
skip: an error at collection aborts the whole suite's run, so a minimal
install could not run the tests it CAN run. The guard follows the
tests/api/conftest.py pattern — the whole directory skips itself at import
time, loudly labelled, and the documented developer install (``.[dev]``,
which carries ``paa[tui]``) is the shape that runs it.
"""

from __future__ import annotations

import pytest

pytest.importorskip("textual", reason="needs the tui extra")
