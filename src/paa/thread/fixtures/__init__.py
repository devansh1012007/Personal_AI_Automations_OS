"""The fake harnesses' package — scripts the bindings execute as files.

The two modules here are **not** imported by the thread engine at runtime;
they are executed as standalone scripts (``<python> fake_alpha.py``) inside
the sandboxed segment, exactly the way a real harness binary would be. They
live under ``src/paa`` as modules anyway — with an ``__init__.py`` and
import-clean module bodies — because the conventions lock sweeps every
``*.py`` under the package and a script that cannot be imported cleanly
would fail it; the ``if __name__ == "__main__"`` guard is what keeps the
sweep and the execution story consistent.
"""

from __future__ import annotations
