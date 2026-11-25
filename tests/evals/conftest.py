"""Shared fixtures for the eval suite (C7.4).

The eval harness's integration rows run real children under the real
subprocess sandbox with a real home — the same shape ``tests/recorder``
uses — because the acceptance line is about the evidence boundary, and a
mocked boundary would prove nothing about it. Everything here is
offline: the shipped harnesses are scripted fakes, and the suite pins
that they stay that way.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated PAA_HOME on the subprocess backend.

    The backend is pinned in the environment (not just in the test's
    own objects) because the mediated rows spawn a proxy grandchild
    that builds its own runtime from these very variables — the child
    must land on the same containment the scorer scored under.
    """
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("PAA_HOME", str(h))
    monkeypatch.setenv("PAA_SANDBOX__BACKEND", "subprocess")
    from paa.config import reset_settings_cache

    reset_settings_cache()
    yield h
    reset_settings_cache()


def src_on_path() -> dict[str, str]:
    """The child env that keeps the sandbox's children on this checkout.

    The sandbox builds the child environment from an explicit allowlist,
    never from ``os.environ``, so a test run under ``PYTHONPATH=…/src``
    does not pass that path down by itself — and the mediated harness's
    proxy grandchild (a fresh ``paa`` CLI process) would import some
    other installed ``paa`` or none at all. The runner's ``child_env``
    seam exists for exactly this; production passes nothing because an
    installed runtime resolves ``paa`` on its own.
    """
    import paa

    src = Path(paa.__file__).resolve().parents[1]
    return {"PYTHONPATH": str(src), "PAA_SANDBOX__BACKEND": "subprocess"}
