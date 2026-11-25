"""The chat cold-start import budget (SPEC 2 §3.2 checklist, "Startup").

The checklist's first line is a budget, not a wish: "Startup: < 2 s cold (lazy
model/LLM imports — pattern already proven in ``cli/main.py``) ... measured,
asserted in CI perf smoke." This file is that measurement for the import-time
half of the budget.

The two modules measured are the two ways a turn is driven without the TUI
framework:

* ``paa.tui.chat.plain`` — the ``--plain`` fallback loop. It must never import
  Textual (that is its contract, stated in its docstring), and it must never
  pay for the runtime stack either: ``paa.cli.chat`` imports it lazily, and an
  import that pulled :mod:`paa.runtime` would make every ``--plain`` start
  pay for SQLite, the ledger and the model layer before the first prompt.
* ``paa.cli.chat`` — the chat sub-app itself. Module scope builds only a
  Typer object; the TUI import, the runtime build and the model wiring all
  stay behind the command callback.

The budget is the test. There is deliberately **no skip** for slow hosts: a
skip would be the quiet way to stop measuring the exact thing the checklist
says must be measured. The ceiling is a generous 2.0 s — roughly an order of
magnitude above what the imports actually take on this repository's CI class
of machine (a couple of hundred milliseconds, dominated by interpreter boot
and the ``pydantic``/``rich`` import graph) — so that a busy host sharing a
core with other jobs still fails only when the *code* has actually become
import-heavy, not when the *host* was momentarily loaded. If this test goes
red, something started importing Textual, the runtime, or a model provider at
module scope, and the fix is to make that import lazy — not to raise the
ceiling.

Measured as subprocess wall-clock (:func:`time.perf_counter` around
``subprocess.run``), because in-process ``importlib`` timing would measure
this suite's already-warm interpreter and the very modules it has loaded —
the opposite of a cold start.
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

#: The cold-start import budget in seconds (SPEC 2 §3.2). Generous on
#: purpose — see the module docstring for why 2.0 and not 0.5.
BUDGET_SECONDS = 2.0

#: The modules that must stay import-light, each with the reason it exists.
COLD_MODULES: tuple[str, ...] = (
    "paa.tui.chat.plain",
    "paa.cli.chat",
)


@pytest.mark.slow
@pytest.mark.parametrize("module", COLD_MODULES)
def test_import_stays_under_the_cold_start_budget(module: str) -> None:
    """Importing the plain chat path costs well under the 2 s cold-start budget."""
    started = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        check=False,
    )
    elapsed = time.perf_counter() - started

    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert elapsed < BUDGET_SECONDS, (
        f"`import {module}` took {elapsed:.2f}s — over the {BUDGET_SECONDS}s "
        "cold-start budget (SPEC 2 §3.2). Something became import-heavy at "
        "module scope; make it lazy instead of raising the budget."
    )
