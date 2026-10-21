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
from typing import Any, NamedTuple

import pytest

from paa.config import ModelSettings, SandboxSettings, Settings, reset_settings_cache

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
    # The model settings the compose file forwards. They are forwarded so a
    # credential or a provider choice made in the operator's shell reaches the
    # container (ADR-0030), and a forwarded variable is a variable the
    # runtime reads — so it has to land in a real field.
    "PAA_MODELS__LOCAL_PROVIDER": "models.local_provider",
    "PAA_MODELS__LOCAL_MODEL": "models.local_model",
    "PAA_MODELS__ESCALATION_PROVIDER": "models.escalation_provider",
}

_ENVIRONMENT_BLOCK = re.compile(r"^(\s+)environment:\s*$")
_ENV_ENTRY = re.compile(r"^\s+([A-Za-z_][A-Za-z0-9_]*):\s*(.*?)\s*$")
_ENV_LIST_ENTRY = re.compile(r"^\s+-\s+([A-Za-z_][A-Za-z0-9_]*)=(.*?)\s*$")
_INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?-([^}]*))?\}")

#: A service key, at two-space indent.
_SERVICE_KEY = re.compile(r"^  ([A-Za-z0-9_.-]+):\s*(#.*)?$")
#: A ``build:`` block, at four-space indent.
_BUILD_KEY = re.compile(r"^    build:\s*(#.*)?$")
#: An entry of a ``build:`` block, at six-space indent.
_BUILD_ENTRY = re.compile(r"^      ([A-Za-z0-9_.-]+):\s*(.*?)\s*$")
#: An ``image:`` key, at four-space indent.
_IMAGE_KEY = re.compile(r"^    image:\s*(.*?)\s*$")


class _Service(NamedTuple):
    """What a compose service declares about itself."""

    image: str
    build: dict[str, str]


def _services(path: Path) -> dict[str, _Service]:
    """Every service in a compose file, with its ``image:`` and ``build:``.

    Read as text, like every other reader in this directory: PyYAML is not a
    dependency of this project, and the compose file's shape is regular
    enough to read directly — a service is a key at two-space indent under
    ``services:``, and its own keys are at four.

    Only the ``services:`` map is read. ``networks:`` and ``volumes:`` sit
    at the same indent as a service name and are not services.
    """
    services: dict[str, _Service] = {}
    service = ""
    image = ""
    build: dict[str, str] = {}
    in_build = False
    in_services = False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line[0].isspace():
            in_services = stripped == "services:"
            continue
        if not in_services:
            continue
        named = _SERVICE_KEY.match(line)
        if named is not None:
            if service:
                services[service] = _Service(image=image, build=build)
            service = named.group(1)
            image = ""
            build = {}
            in_build = False
            continue
        if not service:
            continue
        if _BUILD_KEY.match(line):
            in_build = True
            continue
        declared = _IMAGE_KEY.match(line)
        if declared is not None:
            image = declared.group(1).strip().strip("\"'")
            in_build = False
            continue
        if in_build:
            entry = _BUILD_ENTRY.match(line)
            if entry is None:
                in_build = False
                continue
            build[entry.group(1)] = entry.group(2).strip().strip("\"'")
    if service:
        services[service] = _Service(image=image, build=build)
    return services


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


def test_the_forwarded_model_settings_fall_back_to_the_code_default() -> None:
    """The compose file's fallback for a forwarded setting is the code's own.

    A variable in the compose ``environment:`` block always reaches the
    container, so an unset one has to carry a *usable* value. An empty
    provider name is not a usable one: pydantic-settings reads
    ``PAA_MODELS__LOCAL_PROVIDER=""`` as the provider named "", and the
    router then fails to resolve a provider at all — the container dies at
    boot on a variable the operator never set.

    The fallback is therefore ``ModelSettings``' own default, and this test
    is what keeps the copy in the compose file from drifting away from the
    code it is a copy of.

    The default is read from the *field*, not from a live ``Settings()``: a
    live one reads the developer's own ``.env``, and a test whose answer
    depends on the developer's ``.env`` is a test that passes on one host
    and fails on another.
    """
    environment = _paa_environment(COMPOSE)
    for name, field in (
        ("PAA_MODELS__LOCAL_PROVIDER", "local_provider"),
        ("PAA_MODELS__LOCAL_MODEL", "local_model"),
        ("PAA_MODELS__ESCALATION_PROVIDER", "escalation_provider"),
    ):
        raw = environment.get(name)
        assert raw is not None, f"{name} is not forwarded to the container"
        rendered = _rendered(raw)
        expected = ModelSettings.model_fields[field].default
        assert rendered == expected, (
            f"the compose file falls back to {rendered!r} for {name}, but the "
            f"code's own default is {expected!r} — a deployment that sets "
            "neither would run on a different configuration than the one the "
            "runtime would use on a laptop"
        )


def test_paa_init_builds_exactly_what_paa_builds() -> None:
    """``paa-init`` builds the same image as ``paa``, from the same section.

    ``paa-init`` used to declare an ``image:`` and no ``build:`` section, so
    Compose's contract for it was "pull this" — from a registry that has
    never heard of the image. It worked only by accident: the ``paa``
    service's build produced the same tag in the same invocation, so the
    image was in the local store by the time ``paa-init`` started. Starting
    ``paa-init`` alone — which is exactly what an operator does to fix the
    data directory's ownership — failed with a pull error.

    The two services therefore have to declare the **same** build section
    and the **same** image name: a different Dockerfile would build a
    different image under the same tag, and a different tag would build two
    images where there is one.
    """
    services = _services(COMPOSE)
    assert "paa-init" in services, "the compose file has no paa-init service"
    assert "paa" in services, "the compose file has no paa service"
    init = services["paa-init"]
    runtime = services["paa"]
    assert init.build, (
        "paa-init has no build: section — Compose reads that as 'pull this "
        "image', from a registry that has never heard of it"
    )
    assert init.build == runtime.build, (
        f"paa-init builds {init.build} and paa builds {runtime.build} — they "
        "have to be the same build, or the same tag names two images"
    )
    assert init.image == runtime.image, (
        f"paa-init builds {init.image!r} and paa builds {runtime.image!r} — "
        "the same build has to produce the same tag"
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
