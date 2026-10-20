"""The compose files' environment must actually boot the settings.

A variable in ``docker-compose.yml`` is a *promise* about the runtime's
configuration. If the promise is malformed, the container dies during
settings import — before it can serve a request, and before it can run the
``paa doctor`` that would explain why.

That is not hypothetical. ``PAA_SANDBOX__HOST_PATH_MAP`` was a
``tuple[tuple[str, str], ...]`` field, which pydantic-settings decodes from
an environment variable as **JSON**. The compose file said
``/data=/srv/paa/data``, which is not JSON, so every deployment of the
server topology died on boot with a message that named neither the variable
nor the format.

These tests read the compose files as text and assert that what they declare
is something the settings layer can consume. No YAML parser is needed — and
none is wanted: PyYAML is not a dependency of this project, and adding one
for a test would be a worse trade than reading the two blocks directly.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest

from paa.config import SandboxSettings, Settings, reset_settings_cache

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "docker-compose.yml"
COMPOSE_GVISOR = REPO_ROOT / "docker-compose.gvisor.yml"
COMPOSE_FILES = (
    COMPOSE,
    COMPOSE_GVISOR,
)

#: Every ``PAA_*`` variable the compose files set, and the settings field it
#: has to land in. A variable with no field is a variable the runtime reads
#: from nowhere.
#:
#: The paths are *resolved* against :class:`Settings` below, not merely
#: listed: a mapping that names a field which does not exist is a mapping
#: that reads as if it were checked and is not.
_EXPECTED_FIELDS = {
    "PAA_HOME": "home",
    "PAA_API_HOST": "api_host",
    "PAA_API_PORT": "api_port",
    "PAA_STORAGE__BACKEND_RELATIONAL": "storage.backend_relational",
    "PAA_STORAGE__POSTGRES_DSN": "storage.postgres_dsn",
    "PAA_STORAGE__BACKEND_QUEUE": "storage.backend_queue",
    "PAA_STORAGE__REDIS_URL": "storage.redis_url",
    "PAA_STORAGE__BACKEND_VECTOR": "storage.backend_vector",
    "PAA_STORAGE__QDRANT_URL": "storage.qdrant_url",
    "PAA_STORAGE__BACKEND_COLDLAKE": "storage.backend_coldlake",
    "PAA_STORAGE__S3_ENDPOINT": "storage.s3_endpoint",
    "PAA_STORAGE__S3_BUCKET": "storage.s3_bucket",
    "PAA_STORAGE__S3_SECURE": "storage.s3_secure",
    # The S3 credentials are NOT settings fields. Config stores the *names*
    # of the environment variables that hold them
    # (`storage.s3_access_key_env`), and the values are read from the
    # environment by name — so these two variables are credential
    # *indirection*, and the right assertion is that the names they point at
    # are the ones config expects.
    "PAA_S3_ACCESS_KEY": "storage.s3_access_key_env",
    "PAA_S3_SECRET_KEY": "storage.s3_secret_key_env",
    "PAA_ALLOW_NON_LOOPBACK": "allow_non_loopback",
    "PAA_SANDBOX__CONTAINER_RUNTIME": "sandbox.container_runtime",
    "PAA_SANDBOX__BACKEND": "sandbox.backend",
    "PAA_SANDBOX__HOST_PATH_MAP": "sandbox.host_path_map",
}

_ENVIRONMENT_BLOCK = re.compile(r"^(\s+)environment:\s*$")
_ENV_ENTRY = re.compile(r"^\s+([A-Za-z_][A-Za-z0-9_]*):\s*(.*?)\s*$")
_ENV_LIST_ENTRY = re.compile(r"^\s+-\s+([A-Za-z_][A-Za-z0-9_]*)=(.*?)\s*$")
_INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?-([^}]*))?\}")


def _paa_environment(path: Path) -> dict[str, str]:
    """The ``PAA_*`` variables a compose file sets, with their raw values.

    Only the ``environment:`` blocks are read, and both forms Compose accepts
    are handled — the mapping form (``KEY: value``) and the list form
    (``- KEY=value``) — because a file that silently stops being parsed is
    worse than one that is never parsed. The value is taken verbatim:
    ``${VAR}`` interpolation is Compose's job and happens before the runtime
    sees it, so substituting it here would hide exactly the kind of
    malformed value these tests exist to catch.
    """
    found: dict[str, str] = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        block = _ENVIRONMENT_BLOCK.match(lines[index])
        if block is None:
            index += 1
            continue
        indent = len(block.group(1))
        index += 1
        while index < len(lines):
            line = lines[index]
            if not line.strip():
                index += 1
                continue
            if line.lstrip().startswith("#"):
                # A comment inside the block — the compose files carry
                # explanatory ones — is not the end of the block.
                index += 1
                continue
            listed = _ENV_LIST_ENTRY.match(line)
            if listed is not None:
                key, _, value = listed.group(1).partition("=")
                if key.startswith("PAA_"):
                    found[key] = value.strip().strip("\"'")
                index += 1
                continue
            entry = _ENV_ENTRY.match(line)
            if entry is None:
                break
            # A mapping entry is one level deeper than the block's key.
            if len(line) - len(line.lstrip()) <= indent:
                break
            key, value = entry.group(1), entry.group(2)
            if key.startswith("PAA_"):
                found[key] = value.strip("\"'")
            index += 1
    return found


def _rendered(value: str) -> str:
    """What Compose actually hands the container for an interpolated value.

    ``${NAME:-default}`` becomes the default — which is what a deployment
    that has not overridden the variable gets — and a bare ``${NAME}``
    becomes a placeholder, since its value is the operator's business.
    """
    return _INTERPOLATION.sub(
        lambda m: m.group(2) if m.group(2) is not None else "placeholder", value
    )


def test_every_compose_paa_variable_lands_in_a_settings_field() -> None:
    """A ``PAA_*`` variable with no field is a variable nobody reads.

    This is the check that would have caught the host-path map: the field
    existed, but its *type* could not hold what the compose file said.

    The dotted path is **resolved** against the real settings object, so a
    mapping that names a field which does not exist fails here rather than
    passing because the name was spelled plausibly.
    """
    settings = Settings()
    for path in COMPOSE_FILES:
        for name in _paa_environment(path):
            assert name in _EXPECTED_FIELDS, (
                f"{name} is set by {path.name} but no settings field is named "
                "for it — it will be ignored at runtime"
            )
            target: Any = settings
            for part in _EXPECTED_FIELDS[name].split("."):
                assert hasattr(target, part), (
                    f"{name} is mapped to "
                    f"{_EXPECTED_FIELDS[name]!r}, but {part!r} is not a field of "
                    f"{type(target).__name__}"
                )
                target = getattr(target, part)


def test_the_settings_accept_the_host_path_map_the_compose_file_sets() -> None:
    """The value the compose file ships must parse.

    This is the regression guard for the boot failure: the map is a *string*
    field, so ``/data=/srv/paa/data`` is read verbatim and parsed by the
    settings layer. If the field ever goes back to a complex type,
    pydantic-settings will demand JSON and this test fails.
    """
    seen = False
    for path in COMPOSE_FILES:
        raw = _paa_environment(path).get("PAA_SANDBOX__HOST_PATH_MAP")
        if raw is None:
            continue
        seen = True
        settings = Settings(sandbox=SandboxSettings(host_path_map=_rendered(raw)))
        assert settings.sandbox.host_path_pairs, (
            f"the compose file's host path map {raw!r} parsed to nothing"
        )
        for inside, host in settings.sandbox.host_path_pairs:
            assert inside.startswith("/")
            assert host.startswith("/")
    assert seen, "no compose file declares PAA_SANDBOX__HOST_PATH_MAP"


def test_the_host_path_map_survives_the_environment() -> None:
    """The same value, this time through the environment layer.

    ``pydantic-settings`` decodes a complex-typed field from an environment
    variable as JSON, so the *type* of the field decides whether the compose
    file's value is readable at all. Going through the environment is the
    only way to prove the whole path works.
    """
    raw = "/data=/srv/paa/data"
    os.environ["PAA_SANDBOX__HOST_PATH_MAP"] = raw
    try:
        reset_settings_cache()
        settings = Settings()
        assert settings.sandbox.host_path_pairs == (("/data", "/srv/paa/data"),)
    finally:
        del os.environ["PAA_SANDBOX__HOST_PATH_MAP"]
        reset_settings_cache()


def test_a_malformed_host_path_map_is_reported_rather_than_ignored() -> None:
    """A broken map must be visible, not silently dropped.

    Dropping the entry means the Docker daemon is handed a path it does not
    recognise and answers by creating an empty, root-owned directory in its
    place — a wrong mount that looks fine.
    """
    settings = SandboxSettings(host_path_map="/data:/srv/paa/data")
    assert settings.host_path_map_error is not None


@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_host_path_map_is_not_an_error(value: str) -> None:
    """No map means "the same path", which is the laptop's answer."""
    settings = SandboxSettings(host_path_map=value)
    assert settings.host_path_map_error is None
    assert settings.host_path_pairs == ()


def test_the_compose_file_pins_the_sandbox_backend() -> None:
    """The server topology must not silently degrade to host-process execution.

    ``PAA_SANDBOX__BACKEND`` left on ``auto`` means the backend is chosen by
    a probe, and the most likely reason the probe fails here is the socket's
    GID being wrong — the misconfiguration this file is itself responsible
    for making possible. On ``auto`` that turns containerised plan steps
    into host-process execution while the deployment looks healthy.

    The value is interpolated from the environment so an operator can still
    override it (see the next test); the point is that the *default* is the
    container backend, not the probe.
    """
    environment = _paa_environment(COMPOSE)
    assert "PAA_SANDBOX__BACKEND" in environment
    assert "docker" in environment["PAA_SANDBOX__BACKEND"]


def test_the_pinned_sandbox_variables_can_be_overridden_from_the_environment() -> None:
    """The pin must not be a trapdoor with no way back.

    A variable in the compose ``environment:`` block takes precedence over
    ``env_file``, so a hard-coded value cannot be changed by ``.env`` at all.
    Interpolating it (``${VAR:-default}``) keeps the safe default *and*
    leaves the operator a way to choose something else.
    """
    for name in ("PAA_SANDBOX__BACKEND", "PAA_SANDBOX__CONTAINER_RUNTIME"):
        raw = _paa_environment(COMPOSE).get(name, "")
        assert "${" in raw, (
            f"{name} is hard-coded in the compose file, so .env cannot "
            "override it — the safe default has to be interpolated"
        )


def test_the_boot_probe_bound_is_longer_than_the_backends_own_timeout() -> None:
    """Cancellation must be the *rare* path, not the normal one.

    The boot sequence bounds the sandbox probe so a wedged daemon cannot
    stall it. If that bound is shorter than the backend's own probe timeout,
    the outer bound cancels the inner wait instead — and a cancelled wait
    leaves the daemon's child process running, so every boot against a slow
    daemon leaks a process.

    The inner probe has to give out first, so that it can clean up after
    itself.
    """
    from paa.runtime import _SANDBOX_PROBE_BOUND_SECONDS
    from paa.sandbox.docker_backend import _HEALTHCHECK_TIMEOUT_SECONDS

    assert _SANDBOX_PROBE_BOUND_SECONDS > _HEALTHCHECK_TIMEOUT_SECONDS
