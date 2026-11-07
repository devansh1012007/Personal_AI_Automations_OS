"""Static lock on Dockerfile.embed (R-W2-107).

The derived embeddings image is verified in CI by building it, but the
shape decisions that make it buildable are text properties: the exact
base pin, the retry convention, the pip budget, `pip check` last, no
offline install, and the USER chain. A "simplification" that drops any
of them passes grep review and only goes red at the CI build — so this
suite pins them in the normal test run, following the repo-root path
pattern in ``test_compose_config.py``. No docker is needed or invoked.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EMBED_DOCKERFILE = REPO_ROOT / "Dockerfile.embed"


def _text() -> str:
    return EMBED_DOCKERFILE.read_text(encoding="utf-8")


def _lines() -> list[str]:
    return _text().splitlines()


def test_base_image_is_pinned_exactly() -> None:
    """The pin is the contract (ADR-0042): `FROM paa/runtime:4.1`, exactly."""
    from_lines = [line for line in _lines() if line.startswith("FROM ")]
    assert from_lines == ["FROM paa/runtime:4.1"]


def test_install_carries_the_retry_convention() -> None:
    """Every network op retries: the outer loop plus pip's own budget."""
    text = _text()
    assert "retry() {" in text
    assert "PIP_RETRIES=10" in text
    assert "PIP_TIMEOUT=120" in text


def test_pip_check_is_the_last_pip_command() -> None:
    """`pip check` is the last pip invocation, after the install.

    The `rm -rf /build` cleanup that follows it is not a pip command,
    so the check still runs before the build is considered done.
    """
    pip_lines = [line.strip() for line in _lines() if "pip " in line.strip()]
    assert pip_lines, "no pip invocation found in Dockerfile.embed"
    assert "pip check" in pip_lines[-1], pip_lines
    assert any("pip install" in line for line in pip_lines[:-1])


def test_no_offline_install_in_a_run_line() -> None:
    """`--no-index` may appear only in comments, never in a RUN line.

    The derived image has no wheelhouse to install offline from
    (ADR-0042); an offline flag here would make the build fail.
    """
    for line in _lines():
        if "--no-index" in line:
            assert line.lstrip().startswith("#"), line


def test_user_chain_root_for_install_paa_for_runtime() -> None:
    """R-W2-002: USER root before the install, USER paa after it.

    The base runtime stage ends with `USER paa` over a root-owned
    /opt/venv, so the install and the /build cleanup must run as root —
    and the shipped runtime stage must never run as root.
    """
    lines = _lines()
    user_root = [i for i, line in enumerate(lines) if line.strip() == "USER root"]
    user_paa = [i for i, line in enumerate(lines) if line.strip() == "USER paa"]
    install = next(
        i for i, line in enumerate(lines) if "retry /opt/venv/bin/pip install" in line
    )
    assert user_root, "Dockerfile.embed has no `USER root` before the install"
    assert user_paa, "Dockerfile.embed has no `USER paa` after the install"
    assert user_root[0] < install < user_paa[-1]
