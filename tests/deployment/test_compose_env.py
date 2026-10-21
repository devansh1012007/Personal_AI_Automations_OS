"""No credential in the compose stack can silently become empty.

Compose's interpolation has two failure modes for a variable an operator
forgot to set. ``${VAR}`` substitutes the **empty string** and prints a
warning; the stack then starts, looks healthy, and fails at first use.
``${VAR:?message}`` refuses to start and says which variable is missing.

The distinction matters most for a credential. A stack that comes up with an
empty ``PAA_S3_ACCESS_KEY`` is a stack whose object store rejects every
write — and the operator finds out from an S3 403 much later, not from the
deploy that was supposed to be the moment of failure.

That is not hypothetical. ``docker-compose.yml`` interpolated ``${S3_ROOT_USER}``
bare, and ``.env.example`` claimed the pre-2026 ``MINIO_*`` names were still
accepted. Compose had no fallback for them, so a stale ``.env`` produced a
*silently empty* credential and a stack that looked fine and could not reach
its object store.

So the promise is checked in both directions:

* every credential substitution in the compose files is **required**, so an
  unset variable stops the deploy instead of emptying it;
* every variable compose requires is **documented** in ``.env.example``, so
  the operator is told what to set before the deploy refuses to start;
* ``.env.example`` does not claim the legacy names are accepted, and compose
  does not accept them — the promise and the code cannot drift apart again.

Static and dependency-free. PyYAML is not a dependency of this project, and
``tests/deployment/test_compose_config.py`` already established that the
compose files are read as text rather than parsed. No Docker daemon is
needed either, so this runs everywhere the suite runs.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "docker-compose.yml"
COMPOSE_GVISOR = REPO_ROOT / "docker-compose.gvisor.yml"
COMPOSE_FILES = (
    COMPOSE,
    COMPOSE_GVISOR,
)
ENV_EXAMPLE = REPO_ROOT / ".env.example"

#: The environment variables that carry the object store's credential, and the
#: compose keys that carry them. Two of them reach the runtime (which reads
#: the *names* in ``StorageSettings.s3_access_key_env``), and two reach the
#: store itself — the same secret, handed to both sides of the connection.
#:
#: The names are listed rather than derived, because a check that derives its
#: own subject cannot notice the subject being renamed out from under it.
_CREDENTIAL_KEYS = frozenset(
    {
        "PAA_S3_ACCESS_KEY",
        "PAA_S3_SECRET_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    }
)

#: The database's own credentials, which reach the server and the runtime's
#: DSN. They are a second, separate secret with the same failure mode: a bare
#: ``${VAR}`` leaves the DSN with no password, and the stack comes up unable
#: to reach its database.
_DATABASE_CREDENTIAL_KEYS = frozenset(
    {
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
    }
)

#: Every key whose value is a secret the stack cannot work without.
_GUARDED_KEYS = _CREDENTIAL_KEYS | _DATABASE_CREDENTIAL_KEYS

#: Which half of the object-store credential each compose key carries.
#:
#: The runtime and the store are two sides of one connection, so they are
#: given the same *pair* of values — but they spell the two halves
#: differently, because they are different programs. The role is what makes
#: the two comparable: the runtime's access key is the store's access key,
#: and the runtime's secret is the store's secret.
_CREDENTIAL_ROLES = {
    "PAA_S3_ACCESS_KEY": "access",
    "AWS_ACCESS_KEY_ID": "access",
    "PAA_S3_SECRET_KEY": "secret",
    "AWS_SECRET_ACCESS_KEY": "secret",
}

#: The model provider's variables, which the compose file forwards to the
#: container so that a credential or a provider choice made in the operator's
#: *shell* reaches it — not only the value in the project ``.env``.
#:
#: The two credential names are the registry rows' ``api_key_env`` values
#: (``src/paa/models/registry.py``): the model layer reads a key by NAME, the
#: same indirection ``StorageSettings`` uses for the S3 store.
#:
#: They are deliberately **not** in :data:`_GUARDED_KEYS`. A stack with no
#: model provider is a legitimate configuration — the runtime runs on its
#: embedded defaults — and a required key would stop the deployment from
#: starting for a reason the operator did not cause. That is the opposite of
#: the object store's credential, without which the stack cannot work at all.
_MODEL_KEYS = frozenset(
    {
        "OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
        "PAA_MODELS__LOCAL_PROVIDER",
        "PAA_MODELS__LOCAL_MODEL",
        "PAA_MODELS__ESCALATION_PROVIDER",
    }
)

#: A compose service is a key at two-space indent under ``services:``.
_SERVICE = re.compile(r"^  ([A-Za-z0-9_.-]+):\s*$")
#: An ``environment:`` block is a key at four-space indent under a service.
_ENVIRONMENT_BLOCK = re.compile(r"^    environment:\s*$")
#: The mapping form: ``KEY: value``.
_ENV_ENTRY = re.compile(r"^      ([A-Za-z_][A-Za-z0-9_]*):\s*(.*?)\s*$")
#: The list form: ``- KEY=value``.
_ENV_LIST_ENTRY = re.compile(r"^      -\s+([A-Za-z_][A-Za-z0-9_]*)=(.*?)\s*$")

#: Every ``${NAME}`` substitution, with everything after the name — the
#: ``:-default`` or ``:?message`` that decides what an unset variable means.
_INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)([^}]*)\}")
#: The required form specifically: ``${NAME:?message}``.
_REQUIRED = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):\?([^}]*)\}")

#: A line that assigns one of the pre-2026 names. Compose reads no such
#: variable, so an operator who sets one is setting a value nothing reads.
_LEGACY_ASSIGNMENT = re.compile(r"^\s*MINIO_[A-Za-z0-9_]*\s*=", re.MULTILINE)
#: Wording that claims the legacy names still work.
_CLAIMS_ACCEPTANCE = re.compile(
    r"\b(?:accepted|accepts|still\s+work(?:s|ing)|keeps?\s+working)\b",
    re.IGNORECASE,
)
#: The negation that turns the same wording into a migration note.
_NEGATION = re.compile(r"\b(?:not|no\s+longer|never)\b", re.IGNORECASE)


def _environment_entries(path: Path) -> list[tuple[str, str, str]]:
    """Every ``environment:`` entry in a compose file, as (service, key, value).

    Both forms Compose accepts are handled — the mapping form (``KEY: value``)
    and the list form (``- KEY=value``) — because a file that silently stops
    being parsed is worse than one that is never parsed.

    The sibling test in this directory reads the same files, but it answers a
    different question: it wants the ``PAA_*`` keys and the *settings field*
    each one lands in. This one wants the key of the **service** it belongs
    to — the runtime and the object store have to be given the same secret,
    and that is a relation between two blocks, not a mapping — and it wants
    every key, not only the ``PAA_`` ones. So it carries its own reader
    rather than bending the other one into a shape it was not written for.
    """
    entries: list[tuple[str, str, str]] = []
    service = ""
    in_services = False
    lines = path.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        if not line.startswith(" "):
            # A top-level key. Only the one that opens the service map is a
            # service; `networks:` and `volumes:` have keys at the same
            # indent as a service name, and they are not services.
            in_services = stripped == "services:"
            index += 1
            continue
        if in_services:
            named = _SERVICE.match(line)
            if named is not None:
                service = named.group(1)
                index += 1
                continue
        if _ENVIRONMENT_BLOCK.match(line) is None:
            index += 1
            continue
        index += 1
        while index < len(lines):
            entry_line = lines[index]
            if not entry_line.strip() or entry_line.lstrip().startswith("#"):
                # A comment inside the block — the compose files carry
                # explanatory ones — is not the end of the block.
                index += 1
                continue
            if len(entry_line) - len(entry_line.lstrip()) <= 4:
                break
            listed = _ENV_LIST_ENTRY.match(entry_line)
            if listed is not None:
                entries.append((service, listed.group(1), listed.group(2).strip()))
                index += 1
                continue
            mapped = _ENV_ENTRY.match(entry_line)
            if mapped is None:
                break
            entries.append((service, mapped.group(1), mapped.group(2).strip().strip("\"'")))
            index += 1
    return entries


def _blocks(text: str) -> list[str]:
    """The blank-line-separated blocks of a text file."""
    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip():
            current.append(line)
            continue
        if current:
            blocks.append("\n".join(current))
            current = []
    if current:
        blocks.append("\n".join(current))
    return blocks


def test_every_credential_in_compose_is_a_required_variable() -> None:
    """A bare ``${VAR}`` is an empty credential, not a missing one.

    Compose substitutes the empty string for an unset variable and carries
    on, so the stack comes up with an empty key, passes every health check it
    has, and cannot reach its object store. The failure surfaces at the first
    S3 write, which is not the moment the operator is watching.

    ``${VAR:?message}`` refuses to start instead, and names the variable that
    is missing. That is the whole difference between a broken stack and a
    clear error, and it is worth one line of compose per credential.
    """
    entries = [entry for path in COMPOSE_FILES for entry in _environment_entries(path)]
    keys = {key for _, key, _ in entries}
    missing = sorted(_GUARDED_KEYS - keys)
    assert not missing, (
        f"the compose files no longer set {missing} — this check would pass "
        "vacuously, and the credentials would be unguarded"
    )
    for service, key, value in entries:
        if key not in _GUARDED_KEYS:
            continue
        substitutions = _INTERPOLATION.findall(value)
        assert substitutions, (
            f"{service}.{key} is a literal, not a substitution — a credential "
            "written into a compose file is a secret in the repository"
        )
        for name, suffix in substitutions:
            assert suffix.startswith(":?"), (
                f"{service}.{key} substitutes ${{{name}}}{suffix}. A bare "
                f"${{{name}}} becomes an EMPTY STRING when the variable is "
                "unset, and the stack then starts with an empty credential "
                f"and cannot reach its object store. Use ${{{name}:?<message>}} "
                "so the deploy fails instead."
            )
            assert name in suffix, (
                f"{service}.{key}: the required-variable message {suffix!r} "
                f"does not name {name}, so the operator cannot tell what to set"
            )


def test_the_runtime_and_the_object_store_are_given_the_same_variable() -> None:
    """Two sides of one connection, one secret — role by role.

    The runtime authenticates to the object store, so the credential it holds
    and the credential the store expects are the same value. If the two
    services read different variables, one of them is wrong — and the failure
    is an authentication error at first use, not a configuration error at
    deploy time.

    The comparison is made **per role**, not over the set of variables a
    service mentions. A set comparison passes when the two sides are
    *swapped* — the runtime given the secret where the store is given the
    access key — because both services still mention the same two variables.
    A swap is an authentication failure at first use, so it is exactly the
    case this check exists to catch, and it is caught here by role.
    """
    by_service: dict[str, dict[str, str]] = {}
    for path in COMPOSE_FILES:
        for service, key, value in _environment_entries(path):
            role = _CREDENTIAL_ROLES.get(key)
            if role is None:
                continue
            names = [name for name, _ in _INTERPOLATION.findall(value)]
            assert len(names) == 1, (
                f"{service}.{key} substitutes {names} — a credential is one "
                "value, and a second substitution is a mistake"
            )
            by_service.setdefault(service, {})[role] = names[0]
    assert len(by_service) >= 2, (
        f"expected the runtime and the object store to each be given a "
        f"credential, found {sorted(by_service)}"
    )
    for service, roles in by_service.items():
        assert set(roles) == {"access", "secret"}, (
            f"{service} is given {sorted(roles)} — it needs both an access key "
            "and a secret"
        )
    distinct = {tuple(sorted(roles.items())) for roles in by_service.values()}
    assert len(distinct) == 1, (
        "the services are given different credentials: "
        + "; ".join(f"{service} -> {roles}" for service, roles in by_service.items())
    )
    roles = next(iter(by_service.values()))
    assert roles["access"] != roles["secret"], (
        "the access key and the secret are read from the same variable — the "
        "two sides of the connection are given each other's half"
    )


def test_every_required_variable_is_documented_in_the_env_example() -> None:
    """A variable compose demands is a variable the operator is told about.

    The guard makes an unset variable fail the deploy. That is only a
    kindness if the operator knows *which* variable to set, and the file
    they are told to copy is ``.env.example``.
    """
    env_text = ENV_EXAMPLE.read_text(encoding="utf-8")
    required: set[str] = set()
    for path in COMPOSE_FILES:
        text = path.read_text(encoding="utf-8")
        for name, message in _REQUIRED.findall(text):
            required.add(name)
            assert message.strip(), (
                f"{path.name}: ${{{name}:?}} carries no message — the deploy "
                "fails, and the operator is not told what is missing"
            )
    assert required, (
        "no compose variable is guarded with :? — either the guard is gone, "
        "or this check has stopped describing the file"
    )
    for name in sorted(required):
        assert re.search(rf"^{re.escape(name)}\s*=", env_text, re.MULTILINE), (
            f"compose requires {name} to be set, but .env.example never tells "
            "the operator to set it"
        )


def test_the_env_example_does_not_claim_the_legacy_names_are_accepted() -> None:
    """The promise and the code cannot drift apart again.

    The pre-2026 names are not accepted by compose: it reads
    ``S3_ROOT_USER``/``S3_ROOT_PASSWORD`` and has no fallback. A file that
    says otherwise sends an operator with a stale ``.env`` to a deploy that
    looks healthy and cannot reach its object store.
    """
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    for block in _blocks(text):
        if "MINIO" not in block.upper():
            continue
        assert not _LEGACY_ASSIGNMENT.search(block), (
            "the pre-2026 names are not read by compose — .env.example must "
            "not tell an operator to set one"
        )
        if _CLAIMS_ACCEPTANCE.search(block):
            assert _NEGATION.search(block), (
                "a block that mentions the pre-2026 names must not claim they "
                "are accepted: compose has no fallback for them, and a stale "
                ".env then produces a stack with an empty credential"
            )


def test_the_env_example_tells_an_operator_with_a_stale_env_what_to_rename() -> None:
    """The migration note is the only thing that helps a stale ``.env``.

    An operator whose ``.env`` predates the rename has no way to discover the
    new names except by being told. The note has to name both sides of every
    rename, or it leaves them guessing.
    """
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    for legacy, current in (
        ("MINIO_ROOT_USER", "S3_ROOT_USER"),
        ("MINIO_ROOT_PASSWORD", "S3_ROOT_PASSWORD"),
        ("MINIO_BUCKET", "S3_BUCKET"),
    ):
        assert legacy in text, (
            f"{legacy} is not mentioned in .env.example — an operator with a "
            "stale .env cannot discover that it has to be renamed"
        )
        assert current in text, f"{current} is not mentioned in .env.example"


def test_compose_reads_no_legacy_variable() -> None:
    """The code side of the promise: there is no legacy fallback to rely on.

    ``StorageSettings`` still accepts the old ``minio_*`` *setting* names
    (ADR-0026) — that is a different thing, and it is deliberate. The
    *compose* variables are the deployment's own, and keeping two spellings
    alive in them is exactly what the rename was for.
    """
    for path in COMPOSE_FILES:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\$\{MINIO_[A-Za-z0-9_]*", text), (
            f"{path.name} substitutes a pre-2026 variable — the two names "
            "cannot both be alive in the deployment"
        )
        assert not re.search(r"^\s*MINIO_[A-Za-z0-9_]*\s*=", text, re.MULTILINE), (
            f"{path.name} assigns a pre-2026 variable"
        )


def test_the_model_variables_are_forwarded_to_the_container() -> None:
    """The model block reaches the container, and this check is not vacuous.

    The compose file used to deliver the model configuration through
    ``env_file: .env`` alone, which delivers the FILE's value and nothing
    else: the documented ``export OPENROUTER_API_KEY=…`` was silently
    ignored, so a containerised runtime could not reach a provider whose
    credential lived only in the operator's shell.
    """
    forwarded = {
        key
        for path in COMPOSE_FILES
        for _, key, _ in _environment_entries(path)
        if key in _MODEL_KEYS
    }
    assert forwarded == set(_MODEL_KEYS), (
        f"the compose files forward {sorted(forwarded)} — the model variables "
        f"are {sorted(_MODEL_KEYS)}, and the ones that are missing are the ones "
        "the shell cannot reach the container through"
    )


def test_the_model_variables_are_optional_substitutions() -> None:
    """A model provider is OPTIONAL, so its variables are not ``:?``-guarded.

    A stack with no model provider is a legitimate configuration — the
    runtime runs on its embedded defaults — and a required key would stop the
    deployment from starting for a reason the operator did not cause. The
    object store's credential is the opposite case and is guarded; the two
    are different failures and are treated differently on purpose.
    """
    for path in COMPOSE_FILES:
        for service, key, value in _environment_entries(path):
            if key not in _MODEL_KEYS:
                continue
            substitutions = _INTERPOLATION.findall(value)
            assert substitutions, (
                f"{service}.{key} is a literal, not a substitution — the value "
                "would be baked into the compose file"
            )
            for name, suffix in substitutions:
                assert not suffix.startswith(":?"), (
                    f"{service}.{key} substitutes ${{{name}}}{suffix}. A "
                    "required model variable stops the deployment from "
                    "starting, and a deployment with no model provider is a "
                    "legitimate one. Use ${{{name}:-<default>}}."
                )
                assert suffix.startswith(":-"), (
                    f"{service}.{key} substitutes ${{{name}}}{suffix} — a bare "
                    f"${{{name}}} becomes an EMPTY STRING, and an empty "
                    "provider name is not an absent one: the router fails to "
                    "resolve a provider at all."
                )


def test_every_forwarded_model_variable_is_documented_in_the_env_example() -> None:
    """A variable the compose file reads is a variable the operator is told about.

    The file an operator is told to copy is ``.env.example``, and the model
    block is the one they will look in first.
    """
    env_text = ENV_EXAMPLE.read_text(encoding="utf-8")
    names: set[str] = set()
    for path in COMPOSE_FILES:
        for _, key, value in _environment_entries(path):
            if key not in _MODEL_KEYS:
                continue
            names.update(name for name, _ in _INTERPOLATION.findall(value))
    assert names, "no model variable is substituted — this check would pass vacuously"
    for name in sorted(names):
        assert re.search(rf"^{re.escape(name)}\s*=", env_text, re.MULTILINE), (
            f"the compose file forwards {name}, but .env.example never tells "
            "the operator that they can set it"
        )
