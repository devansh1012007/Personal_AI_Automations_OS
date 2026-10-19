"""The ``paa tui`` command — a live terminal dashboard over the runtime.

Interactive counterpart to the one-shot CLI commands in :mod:`paa.cli.main`.
Instead of a goal string on the command line, the operator types commands into
a prompt at the bottom of a full-width dashboard. The package exposes the two
building blocks the CLI layer composes into ``paa tui``:

* :mod:`paa.tui.dispatcher` — pure command parser returning a structured result
* :mod:`paa.tui.runner` — the interactive driver that polls the runtime and
  renders the dashboard until the operator quits

Commands you can type while the dashboard is running:

* ``new <goal>``          submit a task and let the agent work on it
* ``drive <id>``          drive a queued task to completion right now
* ``status <id>``         show the projection of a lineage
* ``ledger <id>``         walk the event chain for <id>
* ``gate <id> [--approve|--reject]``   clear or reject a human gate
* ``doctor``              show which backends are live
* ``help``                this message
* ``q / :q / :quit``      exit the dashboard
"""

from __future__ import annotations

from paa.tui.dispatcher import CommandResult, dispatch
from paa.tui.snapshot import CorrelationRow, Snapshot, parse_correlation_id

__all__ = [
    "CommandResult",
    "CorrelationRow",
    "Snapshot",
    "dispatch",
    "parse_correlation_id",
]
