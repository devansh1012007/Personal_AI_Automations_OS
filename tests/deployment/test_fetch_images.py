"""The fetch script fetches what the compose file actually runs.

``deploy/fetch-images.sh`` used to carry its own hand-copied table of images.
It was a second list of what the deployment needs, and the two drifted: two of
the script's four entries were *older versions* than the ones
``docker-compose.yml`` ran, so the script fetched an image the deployment
never used and left the one it did use for ``docker compose up`` to fetch —
and fail on. Three of the script's other claims were false as well:

* the digest pin was documented and implemented nowhere, so a pulled image was
  pinned by nothing;
* the presence check asked for ``postgres``, which resolves to
  ``postgres:latest`` — a name the daemon does not have — so the script
  reported every image absent even after pulling it successfully;
* ``--load`` consumed two arguments and shifted one, so the branch was
  unreachable dead code, and it would have loaded the tarballs *and then*
  pulled every image.

The script is now a reader of the compose file. **That is only safe if the
reader is right**, and a hand-rolled YAML reader is right only if something
holds it to the file. This module is that something: it parses the compose
file independently, in Python, and asserts that the two agree — and it drives
the script against a **stub ``docker``** to prove the behaviours the compose
file cannot show on its own.

**What the parity test is, and what it is not.** It is a *shape lock on the
current file*: it says that the script's reader and this module's reader
agree about ``docker-compose.yml`` as it stands. It is **not** an
equivalence proof — two readers can agree on one file and disagree on the
next. So the agreement is also asserted over a *corpus of shapes* the
repository's own compose file does not happen to contain: a trailing comment
on an ``image:`` line, a service key with a trailing comment, a service with
neither an ``image:`` nor a ``build:``, an ``image:`` with no value, a
quoted value, a carriage return, and a reference that is a variable. The
corpus is the part that would catch a reader drifting on a shape the
deployment has not used *yet* — and a trailing comment on an ``image:`` line
is exactly the shape where the two readers used to disagree, with the wrong
party being the test: the script strips the comment and the test did not, so
a maintainer "fixing" the script to match the test would have pulled
``postgres:16@sha256:X  # comment``.

Static, dependency-free and daemon-free: PyYAML is not a dependency of this
project, and the stub is a shell script, so the whole module runs wherever
the suite runs. It is skipped on a platform with no bash. It is **not** the
first module in the suite that spawns a process — the sandbox and the skill
suites both do — but it is the first to drive a *shell program* against a
stub on the PATH.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy" / "fetch-images.sh"
COMPOSE = REPO_ROOT / "docker-compose.yml"

#: The script is a bash program. There is no bash on Windows, and a Windows
#: host has no ``/usr/bin/env`` either, so the module is skipped there rather
#: than failing for a reason that has nothing to do with the code.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="deploy/fetch-images.sh is a bash program; this host has no bash",
)

#: A stub ``docker``. It records every call, and it answers ``image
#: inspect`` from the reference it was given — the way a real daemon answers,
#: with the repository path stripped of its tag and the digest carried
#: through.
#:
#: Three things about it are load-bearing:
#:
#: * it answers the ``--format`` template it was given, and it answers
#:   ``.Id`` with a digest that is **not** the manifest digest. ``.Id`` is
#:   the image's content identifier, and what it names depends on the
#:   daemon's image store (ADR-0030), so a check written against it would
#:   pass or fail according to the host. A stub that answered both fields
#:   alike would make the choice invisible — which is exactly how a script
#:   can read ``.Id`` and still pass every test here;
#: * in the ``pulled`` mode it is **stateful**: an image is absent until it
#:   has been pulled, and a ``docker tag`` makes its target resolvable. That
#:   is what lets a pull succeed and be *verified* — the state in which the
#:   script's post-pull decisions (the mirror's tag) are taken;
#: * the ``no-daemon`` mode answers ``docker info`` with a failure, so the
#:   daemon gate can be exercised.
#:
#: The modes are the states the tests need: a daemon that holds the right
#: images, a daemon that holds the wrong ones, a daemon that holds nothing
#: at all, a daemon that holds nothing until it is pulled from, a daemon
#: that is not reachable, and a daemon whose `load` always fails.
_STUB_DOCKER = """#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_LOG"
case "$1" in
  info)
    if [ "$STUB_MODE" = "no-daemon" ]; then
      exit 1
    fi
    exit 0
    ;;
  image)
    ref="$3"
    fmt=""
    shift 3
    for arg in "$@"; do fmt="$arg"; done
    if [ "$STUB_MODE" = "absent" ]; then
      exit 1
    fi
    if [ "$STUB_MODE" = "pulled" ]; then
      grep -qxF "$ref" "$STUB_STORE" 2>/dev/null || exit 1
    fi
    digest=""
    case "$ref" in
      *@sha256:*) digest="${ref##*@}" ;;
    esac
    if [ "$STUB_MODE" = "wrong" ]; then
      digest="sha256:0000000000000000000000000000000000000000000000000000000000000000"
    fi
    if [ -z "$digest" ]; then
      printf '\\n'
      exit 0
    fi
    name="${ref%@*}"
    last="${name##*/}"
    case "$last" in
      *:*) repo="${name%:*}" ;;
      *)   repo="$name" ;;
    esac
    case "$fmt" in
      *RepoDigests*) printf '%s@%s\\n' "$repo" "$digest" ;;
      *".Id"*) printf 'sha256:%s\\n' "$STUB_ID" ;;
      *) printf '%s@%s\\n' "$repo" "$digest" ;;
    esac
    exit 0
    ;;
  pull)
    printf '%s\\n' "$2" >> "$STUB_STORE"
    exit 0
    ;;
  tag)
    digest=""
    case "$2" in
      *@sha256:*) digest="${2##*@}" ;;
    esac
    if [ -n "$digest" ]; then
      printf '%s@%s\\n' "$3" "$digest" >> "$STUB_STORE"
    else
      printf '%s\\n' "$3" >> "$STUB_STORE"
    fi
    exit 0
    ;;
  load)
    if [ "$STUB_MODE" = "load-fails" ]; then
      exit 1
    fi
    exit 0
    ;;
esac
exit 0
"""

#: The digest the stub answers ``.Id`` with. It is deliberately **not** any
#: image's manifest digest: a script that read ``.Id`` would then fail every
#: digest check it makes, which is the point.
_STUB_ID = "1111111111111111111111111111111111111111111111111111111111111111"

#: The external programs the script needs. The list is explicit because the
#: "no docker on the PATH" and "no timeout on the PATH" tests need a PATH
#: that contains everything *except* one thing — and a PATH that contains
#: nothing at all would fail on the first ``awk`` rather than on the thing the
#: test is about.
_TOOLS = ("bash", "awk", "sed", "grep", "sleep", "dirname", "timeout")

#: The Docker commands the script is allowed to use, as
#: ``(verb, subcommand)`` pairs.
#:
#: The pair is the granularity, and the first word alone is not enough:
#: ``docker image prune -f`` and ``docker image rm`` both start with
#: "image", so a first-word check cannot see either of them — and the first
#: of those is the command that would delete somebody else's images.
_ALLOWED_COMMANDS = frozenset(
    {
        ("info", ""),
        ("image", "inspect"),
        ("pull", ""),
        ("tag", ""),
        ("load", ""),
    }
)

#: The verbs whose second argument is a *subcommand* rather than an
#: argument. A verb outside this set has no subcommand, so its second token
#: is whatever it was given — an image reference, a path — and is not part
#: of the command.
_GROUPING_VERBS = frozenset(
    {
        "builder",
        "config",
        "container",
        "context",
        "image",
        "network",
        "node",
        "plugin",
        "secret",
        "service",
        "system",
        "trust",
        "volume",
    }
)

#: Words that must not appear in any recorded call. The commands above are
#: checked exactly, so this is a second, independent net — and it is safe
#: here because every argument the script passes is either an image
#: reference or a path under the test's own temporary directory, neither of
#: which is one of these words.
_FORBIDDEN_WORDS = frozenset(
    {
        "attach",
        "build",
        "buildx",
        "commit",
        "cp",
        "create",
        "diff",
        "events",
        "exec",
        "export",
        "history",
        "import",
        "kill",
        "login",
        "logout",
        "pause",
        "prune",
        "push",
        "restart",
        "rm",
        "rmi",
        "save",
        "start",
        "stop",
        "unpause",
        "update",
        "wait",
    }
)

#: A row of ``--list`` output: the action, the image, and the service.
_LIST_ROW = re.compile(r"^(pull|build)\s+(\S+)\s+\(([^)]+)\)$")

#: A service in the compose file: a key at two-space indent under
#: ``services:``, with no value of its own.
_SERVICE = re.compile(r"^  [^ #\t][^:]*:[ \t]*(#.*)?$")

#: The image a service declares.
_IMAGE = re.compile(r"^    image:[ \t]*(.*?)[ \t]*$")

#: A build section, at the same indent as ``image:``.
_BUILD = re.compile(r"^    build:")

#: A literal digest. The script must not carry one: the pin is read from the
#: compose file, and a digest written into the script is a second place for
#: the pin to be wrong.
_LITERAL_DIGEST = re.compile(r"\b[0-9a-f]{64}\b")

#: A digest for a synthetic compose file. It is a well-formed one and it is
#: not any image's — the tests that use it never reach a real registry.
_DIGEST = "0" * 64

#: The shapes the two readers are compared over. Each one is a compose file
#: the repository does not use, and each one is a place the two readers
#: could drift apart without the build noticing.
#:
#: The value is ``(text, expected exit status)``. The exit status is
#: ``None`` for "as the reader would have it" — the script's own verdict on
#: the shape is asserted separately, where the shape has one.
_PARITY_SHAPES: dict[str, tuple[str, int | None]] = {
    "a plain service": (
        "services:\n  postgres:\n    image: postgres:16@sha256:" + _DIGEST + "\n",
        0,
    ),
    "a trailing comment on the image line": (
        "services:\n  postgres:\n    image: postgres:16@sha256:" + _DIGEST + "  # pinned\n",
        0,
    ),
    "a trailing comment on the service key": (
        "services:\n  postgres:  # the database\n    image: postgres:16@sha256:" + _DIGEST + "\n",
        0,
    ),
    "a service with neither image nor build": (
        "services:\n"
        "  postgres:\n"
        "    image: postgres:16@sha256:" + _DIGEST + "\n"
        "  helper:\n"
        "    command: echo hi\n",
        0,
    ),
    "an image with no value": (
        "services:\n  postgres:\n    image:\n",
        1,
    ),
    "a quoted image": (
        'services:\n  postgres:\n    image: "postgres:16@sha256:' + _DIGEST + '"\n',
        0,
    ),
    "a build section": (
        "services:\n  paa:\n    build:\n      context: .\n    image: paa/runtime:4.1\n",
        0,
    ),
    "carriage returns": (
        "services:\r\n  postgres:\r\n    image: postgres:16@sha256:" + _DIGEST + "\r\n",
        0,
    ),
    "a reference that is a variable": (
        "services:\n  postgres:\n    image: ${POSTGRES_IMAGE:-postgres:16}\n",
        1,
    ),
    "a service named with an inline value": (
        "services:\n  helper: echo hi\n"
        "  postgres:\n    image: postgres:16@sha256:" + _DIGEST + "\n",
        0,
    ),
    "a top-level extension block after services": (
        "services:\n  postgres:\n    image: postgres:16@sha256:" + _DIGEST + "\n"
        "x-extra:\n  whatever: 1\n",
        0,
    ),
}


class _Stub:
    """A stub ``docker`` on a PATH of its own."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir(parents=True, exist_ok=True)
        self.log = root / "docker.log"
        self.store = root / "docker.store"
        self.executable = self.bin / "docker"
        self.executable.write_text(_STUB_DOCKER, encoding="utf-8")
        self.executable.chmod(0o755)

    def calls(self) -> list[str]:
        """Every call the stub was asked to make, in order."""
        if not self.log.exists():
            return []
        return [line for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def raw_log(self) -> bytes:
        """The stub's log, as bytes.

        ``splitlines`` treats a lone carriage return as a line boundary, so a
        reference that still carries one is invisible in :meth:`calls` — it
        looks like a clean line. The bytes are what a check for "no
        carriage return anywhere" has to look at.
        """
        if not self.log.exists():
            return b""
        return self.log.read_bytes()

    def pulls(self) -> list[str]:
        return [call for call in self.calls() if call.startswith("pull ")]

    def loads(self) -> list[str]:
        return [call for call in self.calls() if call.startswith("load ")]

    def tags(self) -> list[str]:
        return [call for call in self.calls() if call.startswith("tag ")]

    def env(self, mode: str = "correct") -> dict[str, str]:
        """The environment the stub reads."""
        return {
            "STUB_MODE": mode,
            "STUB_LOG": str(self.log),
            "STUB_STORE": str(self.store),
            "STUB_ID": _STUB_ID,
        }


def _tool_path(root: Path, *, without: str = "") -> str:
    """A PATH holding the tools the script needs, minus one of them.

    The tools are symlinks into the real PATH, so the stub PATH is a real
    PATH with one program removed rather than a hand-made miniature of one.
    """
    directory = root / "tools"
    directory.mkdir(parents=True, exist_ok=True)
    for name in _TOOLS:
        if name == without:
            continue
        found = shutil.which(name)
        assert found is not None, f"{name} is not on PATH, and the test needs it"
        link = directory / name
        if not link.exists():
            link.symlink_to(found)
    return str(directory)


def _run(
    args: list[str],
    *,
    path: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the script and hand back its result.

    The environment starts from the developer's own and drops the knobs this
    script reads, so a developer's ``PAA_IMAGE_MIRROR`` or a stale
    ``PAA_COMPOSE_FILE`` cannot change what the test exercises.
    """
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PAA_FETCH_", "PAA_COMPOSE_FILE", "PAA_IMAGE_MIRROR", "STUB_"))
    }
    environment["PATH"] = path
    if env:
        environment.update(env)
    return subprocess.run(
        [str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
        cwd=str(REPO_ROOT),
        check=False,
    )


def _service_name(line: str) -> str:
    """The name of a service, as the script's reader reads it.

    Everything between the two-space indent and the first colon — which is
    where a trailing comment can sit, and is therefore not part of the name.
    """
    return line[2:].split(":", 1)[0]


def _image_value(raw: str) -> str:
    """The image a service declares, read the way the script reads it.

    The order is the script's, and it matters: the trailing comment is
    stripped **first** and the quotes second. A reader that stripped the
    quotes first would leave the comment inside the value, and would then
    disagree with the script about the reference it pulls — a disagreement
    that shows up as a pull of ``postgres:16@sha256:X  # comment``.
    """
    value = re.sub(r"[ \t]+#.*$", "", raw)
    if value[:1] in {'"', "'"}:
        value = value[1:]
    if value[-1:] in {'"', "'"}:
        value = value[:-1]
    return value


def _compose_rows(path: Path) -> list[tuple[str, str, str]]:
    """The ``(action, image, service)`` rows a reader of the compose file finds.

    An **independent** implementation of the same rules the script's awk
    reader follows: a service is a key at two-space indent under ``services:``,
    and its ``image:`` and ``build:`` are keys at four-space indent. A service
    with a ``build:`` section is built here; a service with only an ``image:``
    is pulled.

    The point of writing it a second time, in a different language, is that
    the two can be compared. A single reader compared against itself proves
    nothing.

    The rules it follows are the script's, exactly — including the ones that
    are easy to get wrong:

    * a trailing comment is stripped from a service key and from an
      ``image:`` value;
    * a service with neither an ``image:`` nor a ``build:`` is **not** a row;
    * an ``image:`` with no value is reported as ``"-"``, the same as a
      service with no ``image:`` key at all — an empty field in a
      tab-separated row is not a field.
    """
    rows: list[tuple[str, str, str]] = []
    service = ""
    image = ""
    has_image = False
    has_build = False
    in_services = False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line[0].isspace():
            # A top-level key. Only `services:` opens the map that matters.
            in_services = stripped == "services:"
            continue
        if in_services and _SERVICE.match(line):
            if service and (has_image or has_build):
                action = "build" if has_build else "pull"
                rows.append((action, _row_image(image, has_image), service))
            service = _service_name(line)
            image = ""
            has_image = False
            has_build = False
            continue
        if not in_services or not service:
            continue
        declared = _IMAGE.match(line)
        if declared is not None:
            image = _image_value(declared.group(1))
            has_image = True
            continue
        if _BUILD.match(line):
            has_build = True
    if service and (has_image or has_build):
        rows.append(("build" if has_build else "pull", _row_image(image, has_image), service))
    return rows


def _row_image(image: str, has_image: bool) -> str:
    """The image column of a row: the image, or ``"-"`` when there is none."""
    if has_image and image:
        return image
    return "-"


def _list_rows(output: str) -> list[tuple[str, str, str]]:
    """The rows of ``--list`` output, parsed."""
    rows: list[tuple[str, str, str]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        match = _LIST_ROW.match(line)
        assert match is not None, f"unexpected line in the --list output: {line!r}"
        rows.append((match.group(1), match.group(2), match.group(3)))
    return rows


def _command_of(call: str) -> tuple[str, str]:
    """The ``(verb, subcommand)`` pair a recorded Docker call is.

    The subcommand is read only for a verb that takes one: ``image`` takes
    ``inspect``, ``prune`` and ``rm``, and the other two are exactly the
    ones this script must never issue. For every other verb the second
    token is an argument — an image reference, a path — and is not part of
    the command.
    """
    parts = call.split()
    verb = parts[0] if parts else ""
    subcommand = ""
    if verb in _GROUPING_VERBS and len(parts) > 1 and not parts[1].startswith("-"):
        subcommand = parts[1]
    return verb, subcommand


def _write_compose(root: Path, text: str) -> Path:
    """A compose file the test controls."""
    compose = root / "compose.yml"
    compose.write_text(text, encoding="utf-8")
    return compose


def test_the_script_list_agrees_with_the_compose_file() -> None:
    """The script's list IS the compose file's list.

    This is the lock that makes the hand-rolled reader safe. The script
    parses the compose file with ``awk`` and no YAML library, because PyYAML
    is not a dependency of this project; the only thing that makes that
    acceptable is a second, independent reader that says the same thing.
    """
    result = _run(["--list"], path=os.environ.get("PATH", ""))
    assert result.returncode == 0, result.stderr
    assert _list_rows(result.stdout) == _compose_rows(COMPOSE)


@pytest.mark.parametrize("shape", sorted(_PARITY_SHAPES))
def test_the_two_readers_agree_on_every_shape(tmp_path: Path, shape: str) -> None:
    """The two readers agree on the shapes the repository does not use.

    The parity test above is a shape lock on the *current* file: two readers
    can agree about one file and disagree about the next. This is the part
    that catches the drift — a trailing comment on an ``image:`` line, a
    service key with a trailing comment, a service with neither an ``image:``
    nor a ``build:``, an ``image:`` with no value, a quoted value, a carriage
    return, a reference that is a variable.

    The dangerous one is the trailing comment. The script strips it and the
    test's reader used to keep it, so the day somebody added one the parity
    test would have failed with the *test* as the wrong party — and a
    maintainer "fixing" the script to match the test would have produced a
    pull reference of ``postgres:16@sha256:X  # comment``.
    """
    text, expected = _PARITY_SHAPES[shape]
    compose = _write_compose(tmp_path, text)
    result = _run(["--list"], path=_tool_path(tmp_path), env={"PAA_COMPOSE_FILE": str(compose)})
    assert _list_rows(result.stdout) == _compose_rows(compose), (
        f"the script and the test's reader disagree about {shape}"
    )
    if expected is not None:
        assert result.returncode == expected, result.stderr


def test_the_list_names_every_service_the_compose_file_declares() -> None:
    """The list is not empty, and it says the right thing about each service.

    The parity test above would pass on two readers that agree on nothing, so
    the content is asserted as well: the four third-party services are pulled
    and digest-pinned, and the two services this repository builds are built.
    """
    rows = _list_rows(_run(["--list"], path=os.environ.get("PATH", "")).stdout)
    pulled = {service for action, _, service in rows if action == "pull"}
    built = {service for action, _, service in rows if action == "build"}
    assert pulled == {"postgres", "redis", "qdrant", "seaweedfs"}
    assert built == {"paa", "paa-init"}
    for action, image, _ in rows:
        if action == "pull":
            assert "@sha256:" in image, f"{image} is pulled but is not digest-pinned"


def test_the_script_carries_no_hand_copied_image_table() -> None:
    """The script holds no image list of its own.

    The table is the defect: two lists of what the deployment needs is one
    list too many, and the extra one drifted. This is a source-level check
    and it is deliberately crude — the parity test above is the real one —
    but it fails loudly the moment a digest or an image name is written into
    the script.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    for name in (
        "postgres:16",
        "redis:7",
        "qdrant/qdrant:",
        "chrislusf/seaweedfs:",
        "paa/runtime:",
        "paa/base_worker:",
        "paa/toolbox:",
    ):
        assert name not in source, (
            f"the script names {name} — the compose file is the source of truth, "
            "and a copy of it here is how the two drifted"
        )
    assert _LITERAL_DIGEST.search(source) is None, (
        "the script carries a digest of its own — the pin is read from the "
        "compose file"
    )
    assert re.search(r"^\s*(LOCAL_)?IMAGES\s*=", source, re.MULTILINE) is None, (
        "the script still declares an image table"
    )


def test_a_wrong_digest_is_retried_and_then_the_script_fails(tmp_path: Path) -> None:
    """A pull that produced the wrong image is a failed ATTEMPT.

    The download and its verification are one unit, the way the Dockerfiles
    treat a download and its SHA-256 check: a transfer that produced
    something other than the pinned image is not a failed fetch, it is a
    failed attempt, and the next attempt gets to try again. The script must
    therefore pull again, and it must end up failing — an unpinned image in a
    stack that claims to be pinned is the failure the pin exists to prevent.
    """
    stub = _Stub(tmp_path)
    path = f"{_tool_path(tmp_path)}:{stub.bin}"
    result = _run(
        [],
        path=path,
        env={
            **stub.env("wrong"),
            "PAA_FETCH_MAX_ATTEMPTS": "3",
            "PAA_FETCH_BASE_DELAY": "0",
        },
    )
    assert result.returncode != 0, result.stdout
    attempts = [call for call in stub.pulls() if call.startswith("pull postgres:16")]
    assert len(attempts) == 3, f"expected three attempts, got {attempts}"


def test_a_tag_only_pin_is_refused(tmp_path: Path) -> None:
    """A pulled image with no digest is an ERROR, not a warning.

    A tag is a promise about a moving target, and the deployment's own
    compose file already pins every one of its images. A tag-only reference
    in a stack that claims to be pinned is exactly the failure the pin
    exists to prevent, so the script refuses it — and it refuses it BEFORE
    it fetches anything.
    """
    compose = _write_compose(tmp_path, "services:\n  postgres:\n    image: postgres:16\n")
    stub = _Stub(tmp_path)
    path = f"{_tool_path(tmp_path)}:{stub.bin}"
    result = _run(
        [],
        path=path,
        env={
            **stub.env(),
            "PAA_COMPOSE_FILE": str(compose),
        },
    )
    assert result.returncode != 0, result.stdout
    assert "not digest-pinned" in result.stderr
    assert stub.pulls() == [], "the script fetched something it had already refused"


def test_an_image_present_under_the_full_reference_is_skipped(tmp_path: Path) -> None:
    """The presence check uses the reference compose uses.

    The old check asked for ``postgres``, which resolves to
    ``postgres:latest`` — a name the daemon does not have — so the script
    reported every image absent even after pulling it successfully. The
    check now asks for the whole reference, and a daemon that holds it says
    so, and the script skips the pull.
    """
    stub = _Stub(tmp_path)
    path = f"{_tool_path(tmp_path)}:{stub.bin}"
    result = _run(
        [],
        path=path,
        env=stub.env(),
    )
    assert result.returncode == 0, result.stderr
    assert stub.pulls() == [], "the script pulled an image the daemon already had"
    calls = stub.calls()
    assert any(call.startswith("image inspect postgres:16@sha256:") for call in calls), calls
    assert "image inspect postgres" not in calls, (
        "the presence check asked for the short name, which resolves to "
        "postgres:latest — a name the daemon does not have"
    )


def test_the_digest_is_read_from_the_repo_digests_and_not_the_image_id(tmp_path: Path) -> None:
    """The check reads ``RepoDigests``, and the suite proves which one.

    ``.Id`` is the image's *content* identifier, and what it names depends on
    the daemon's image store — the configuration digest on the classic graph
    driver, the target descriptor on the containerd store — so a check
    written against it would pass or fail according to the host rather than
    according to the image (ADR-0030).

    The stub answers the two fields **differently**: ``.RepoDigests`` with
    the manifest digest the reference carries, and ``.Id`` with a digest that
    is not it. A script that read ``.Id`` would therefore fail every check
    it makes — and this test, and the ones beside it, would fail with it.
    """
    stub = _Stub(tmp_path)
    path = f"{_tool_path(tmp_path)}:{stub.bin}"
    result = _run(["--verify"], path=path, env=stub.env())
    assert result.returncode == 0, result.stderr
    formatted = [call for call in stub.calls() if "--format" in call]
    assert formatted, "the script never asked for a formatted inspection"
    for call in formatted:
        assert ".RepoDigests" in call, f"the digest was not read from RepoDigests: {call}"
    for call in stub.calls():
        assert ".Id" not in call, f"the digest was read from the image id: {call}"


def test_load_loads_the_tarballs_and_stops(tmp_path: Path) -> None:
    """``--load`` imports what it was given, and it STOPS.

    The branch was unreachable — it consumed two arguments and shifted one —
    and it would have loaded the tarballs *and then* pulled every image.

    A loaded image is **not** digest-verified, and the script says so: a
    ``docker save`` / ``docker load`` round trip restores an image by its
    configuration, and the manifest digest the registry served is not what
    the load path can be asked about. The pin is enforced on the pull path.
    """
    images = tmp_path / "images"
    images.mkdir()
    (images / "one.tar").write_bytes(b"not a real tarball")
    (images / "two.tar").write_bytes(b"not a real tarball")
    stub = _Stub(tmp_path)
    path = f"{_tool_path(tmp_path)}:{stub.bin}"
    result = _run(
        ["--load", str(images)],
        path=path,
        # The daemon holds NOTHING, so a script that fell through into the
        # fetch would pull — which is exactly the defect this test is for.
        # The retry budget is one attempt and no backoff, so a script that
        # *does* fall through fails quickly instead of sleeping.
        env={
            **stub.env("absent"),
            "PAA_FETCH_MAX_ATTEMPTS": "1",
            "PAA_FETCH_BASE_DELAY": "0",
        },
    )
    assert result.returncode == 0, result.stderr
    assert len(stub.loads()) == 2, stub.calls()
    assert stub.pulls() == [], "--load fell through into the fetch"
    assert "not digest-verified" in result.stderr


def test_a_directory_that_does_not_exist_is_not_an_empty_one(tmp_path: Path) -> None:
    """``--load`` says which of the two failures it is.

    "no .tar files in /nowhere" reads as "the directory is there and it is
    empty", and the operator looks for the wrong thing — a path that does not
    exist and a path with no tarballs in it are two different mistakes, and
    the message has to say which one this is.
    """
    stub = _Stub(tmp_path)
    path = f"{_tool_path(tmp_path)}:{stub.bin}"
    missing = tmp_path / "nowhere"
    result = _run(["--load", str(missing)], path=path, env=stub.env())
    assert result.returncode != 0, result.stdout
    assert "no such directory" in result.stderr
    assert "no .tar files" not in result.stderr

    empty = tmp_path / "empty"
    empty.mkdir()
    result = _run(["--load", str(empty)], path=path, env=stub.env())
    assert result.returncode != 0, result.stdout
    assert "no .tar files" in result.stderr
    assert "no such directory" not in result.stderr


def test_list_needs_neither_the_docker_cli_nor_a_daemon(tmp_path: Path) -> None:
    """``--list`` is a report about a FILE.

    The list comes from the compose file, so it needs no daemon — and the
    gate that demands one has to come *after* the branch, or the report
    cannot be produced on a host that has no Docker at all.
    """
    path = _tool_path(tmp_path)
    result = _run(["--list"], path=path)
    assert result.returncode == 0, result.stderr
    assert _list_rows(result.stdout) == _compose_rows(COMPOSE)


def test_the_list_is_a_gate_and_says_so(tmp_path: Path) -> None:
    """``--list`` exits non-zero on an image it cannot pin.

    It used to only warn, so a plan it printed and a plan it enforced were
    two different things: ``--verify`` and the fetch refuse an unpinned
    image, and ``--list`` — the one that needs no daemon, and is therefore
    the one a CI job can run anywhere — reported success. The list still
    prints (the operator needs to see *what* is wrong) and the exit status
    is the verdict.
    """
    compose = _write_compose(tmp_path, "services:\n  postgres:\n    image: postgres:16\n")
    result = _run(
        ["--list"],
        path=_tool_path(tmp_path),
        env={"PAA_COMPOSE_FILE": str(compose)},
    )
    assert result.returncode != 0, result.stdout
    assert "not digest-pinned" in result.stderr
    assert "postgres:16" in result.stdout, "the list still has to say what is wrong"


def test_a_reference_that_is_a_variable_is_refused_with_an_accurate_message(tmp_path: Path) -> None:
    """A compose variable is refused, and the message says why.

    The script reads the compose file as text; it does not interpolate
    compose's variables, because it cannot know what the operator's shell
    and ``.env`` hold, and a guess would pull something the compose file
    did not name. What it must not do is read ``${IMAGE:-postgres:16}`` as
    an image reference and then advise pinning it as
    ``${IMAGE:-postgres:16}@sha256:<digest>`` — advice that cannot be
    followed, about a reference that is not an image.
    """
    compose = _write_compose(
        tmp_path,
        "services:\n  postgres:\n    image: ${POSTGRES_IMAGE:-postgres:16}\n",
    )
    stub = _Stub(tmp_path)
    path = f"{_tool_path(tmp_path)}:{stub.bin}"
    result = _run(
        [],
        path=path,
        env={**stub.env(), "PAA_COMPOSE_FILE": str(compose)},
    )
    assert result.returncode != 0, result.stdout
    assert "VARIABLE" in result.stderr
    assert "does not" in result.stderr and "interpolate" in result.stderr
    assert "Pin it as" not in result.stderr, (
        "the message advises pinning a variable, which is not a thing anyone "
        "can do"
    )
    assert stub.pulls() == [], "the script fetched something it had already refused"


def test_a_carriage_return_does_not_stay_on_the_digest(tmp_path: Path) -> None:
    """A compose file written on Windows is still a compose file.

    Every line of it ends with a carriage return, and a digest with a
    carriage return on the end of it matches nothing: the reference could
    never be satisfied, and the pull would go out with a reference no
    registry has ever heard of.
    """
    compose = tmp_path / "compose.yml"
    compose.write_bytes(
        ("services:\r\n  postgres:\r\n    image: postgres:16@sha256:" + _DIGEST + "\r\n").encode(
            "utf-8"
        )
    )
    stub = _Stub(tmp_path)
    path = f"{_tool_path(tmp_path)}:{stub.bin}"
    result = _run(
        [],
        path=path,
        env={
            **stub.env("pulled"),
            "PAA_COMPOSE_FILE": str(compose),
            "PAA_FETCH_MAX_ATTEMPTS": "1",
            "PAA_FETCH_BASE_DELAY": "0",
        },
    )
    assert result.returncode == 0, result.stderr
    assert stub.pulls() == ["pull postgres:16@sha256:" + _DIGEST], stub.calls()
    assert b"\r" not in stub.raw_log(), (
        "a carriage return stayed on the reference — the pull would have gone "
        "out with a reference no registry has ever heard of"
    )


def test_an_empty_plan_is_refused(tmp_path: Path) -> None:
    """A plan with no images in it is a broken source of truth.

    The script used to report "every image the compose file names is
    present" about a compose file it had found no images in, and exit 0: a
    gate that had checked nothing, reporting success. A *missing* compose
    file was handled correctly; an empty one was not.
    """
    compose = _write_compose(tmp_path, "services:\n  helper:\n    command: echo hi\n")
    stub = _Stub(tmp_path)
    path = f"{_tool_path(tmp_path)}:{stub.bin}"
    result = _run(
        [],
        path=path,
        env={**stub.env(), "PAA_COMPOSE_FILE": str(compose)},
    )
    assert result.returncode != 0, result.stdout
    assert "no images found" in result.stderr
    assert stub.pulls() == [], "the script fetched something out of an empty plan"


def test_the_script_fails_clearly_when_timeout_is_absent(tmp_path: Path) -> None:
    """The GNU requirement is stated, and it is checked.

    The script bounds every pull with GNU coreutils' ``timeout``. A host
    without it has to be told that, in a sentence about the requirement —
    not with "command not found" in the middle of a pull.
    """
    path = _tool_path(tmp_path, without="timeout")
    result = _run([], path=path)
    assert result.returncode != 0, result.stdout
    assert "timeout" in result.stderr
    assert "GNU coreutils" in result.stderr


def test_the_script_fails_clearly_when_the_docker_cli_is_absent(tmp_path: Path) -> None:
    """The docker CLI is required, and the failure says so.

    A script that ran `docker` and got "command not found" from the shell
    would be a script whose failure names the wrong thing — and the
    requirement is a requirement, so it is checked and stated.
    """
    result = _run([], path=_tool_path(tmp_path))
    assert result.returncode != 0, result.stdout
    assert "docker is not on PATH" in result.stderr


def test_the_script_refuses_to_work_without_a_daemon(tmp_path: Path) -> None:
    """The daemon gate is a gate, and nothing is fetched without one.

    A host with no daemon is a host the script cannot verify anything on,
    and a script that carried on would pull into a store it cannot read
    back — the whole point of the digest check is that it runs against the
    daemon.
    """
    stub = _Stub(tmp_path)
    path = f"{_tool_path(tmp_path)}:{stub.bin}"
    result = _run([], path=path, env=stub.env("no-daemon"))
    assert result.returncode != 0, result.stdout
    assert "daemon is not reachable" in result.stderr
    assert stub.pulls() == [], "the script pulled with no daemon to pull into"


def test_a_tarball_that_will_not_load_is_retried(tmp_path: Path) -> None:
    """``--load`` gets the same retry policy as the pull.

    A tarball that will not load is a transient failure like any other: a
    truncated file, a busy disk. The load is retried, and a persistent
    failure stops the script rather than leaving the operator to wonder
    whether the image arrived.
    """
    images = tmp_path / "images"
    images.mkdir()
    (images / "one.tar").write_bytes(b"not a real tarball")
    stub = _Stub(tmp_path)
    path = f"{_tool_path(tmp_path)}:{stub.bin}"
    result = _run(
        ["--load", str(images)],
        path=path,
        env={
            **stub.env("load-fails"),
            "PAA_FETCH_MAX_ATTEMPTS": "2",
            "PAA_FETCH_BASE_DELAY": "0",
        },
    )
    assert result.returncode != 0, result.stdout
    assert len(stub.loads()) == 2, stub.calls()


def test_verify_reports_the_digest_of_what_is_present(tmp_path: Path) -> None:
    """``--verify`` is the mode that answers "is what I have the right thing?".

    It is a *report*, so it must fail when the answer is no: an image that is
    present under a different digest is not the image the deployment pinned.
    """
    stub = _Stub(tmp_path)
    path = f"{_tool_path(tmp_path)}:{stub.bin}"
    good = _run(
        ["--verify"],
        path=path,
        env=stub.env(),
    )
    assert good.returncode == 0, good.stderr
    bad = _run(
        ["--verify"],
        path=path,
        env=stub.env("wrong"),
    )
    assert bad.returncode != 0, bad.stdout
    assert "is not present with the digest" in bad.stderr


def test_the_mirror_changes_the_path_and_keeps_the_digest(tmp_path: Path) -> None:
    """A mirror is a prefix, and the pin travels with the pull.

    The mirror changes the *repository path* — the official images come from
    ``library/`` and the rest keep their own — and it changes nothing else. In
    particular the digest stays on the reference that is pulled, because a
    pull of a tag is not a pull of a pinned image.
    """
    stub = _Stub(tmp_path)
    path = f"{_tool_path(tmp_path)}:{stub.bin}"
    _run(
        [],
        path=path,
        env={
            **stub.env("absent"),
            "PAA_IMAGE_MIRROR": "registry.example.com",
            "PAA_FETCH_MAX_ATTEMPTS": "1",
            "PAA_FETCH_BASE_DELAY": "0",
        },
    )
    pulled = {call.removeprefix("pull ") for call in stub.pulls()}
    for _, image, service in _compose_rows(COMPOSE):
        if service not in {"postgres", "qdrant"}:
            continue
        name, _, digest = image.partition("@")
        repository = name.rsplit(":", 1)[0]
        if "/" not in repository:
            expected = f"registry.example.com/library/{name}@{digest}"
        else:
            expected = f"registry.example.com/{name}@{digest}"
        assert expected in pulled, f"the mirror pull for {service} was not {expected}"


def test_without_a_mirror_nothing_is_tagged(tmp_path: Path) -> None:
    """A pull that is already the compose reference is not tagged.

    The tag the script issues under a mirror gives the pulled image the
    repository path compose expects. Without a mirror the pulled reference
    **is** the reference compose uses, so there is nothing to alias — and
    the alias would be a MUTABLE one: a tag carrying no digest, which a
    later unpinned pull could move to different bytes. That is the unadvertised
    alias the rewrite introduced, in a deployment whose whole point is that
    the pin is not a promise.
    """
    compose = _write_compose(
        tmp_path,
        "services:\n  postgres:\n    image: postgres:16@sha256:" + _DIGEST + "\n",
    )
    stub = _Stub(tmp_path)
    path = f"{_tool_path(tmp_path)}:{stub.bin}"
    result = _run(
        [],
        path=path,
        env={
            **stub.env("pulled"),
            "PAA_COMPOSE_FILE": str(compose),
            "PAA_FETCH_MAX_ATTEMPTS": "1",
            "PAA_FETCH_BASE_DELAY": "0",
        },
    )
    assert result.returncode == 0, result.stderr
    assert stub.pulls() == ["pull postgres:16@sha256:" + _DIGEST], stub.calls()
    assert stub.tags() == [], f"the script tagged {stub.tags()} with no mirror in play"


def test_with_a_mirror_the_pulled_image_is_tagged_once(tmp_path: Path) -> None:
    """Under a mirror the tag is issued — once, and for the mirror's image.

    The mirror changed the image's repository path, so the pulled image is
    not the reference compose uses, and it needs the name compose expects.
    The tag is issued for the *pinned* image: the reference compose looks up
    is ``<repo>:<tag>@sha256:X``, and the alias the script creates is
    ``<repo>:<tag>`` — the tag half of it, with the digest still carried by
    the image the tag points at.
    """
    compose = _write_compose(
        tmp_path,
        "services:\n  postgres:\n    image: postgres:16@sha256:" + _DIGEST + "\n",
    )
    stub = _Stub(tmp_path)
    path = f"{_tool_path(tmp_path)}:{stub.bin}"
    result = _run(
        [],
        path=path,
        env={
            **stub.env("pulled"),
            "PAA_COMPOSE_FILE": str(compose),
            "PAA_IMAGE_MIRROR": "registry.example.com",
            "PAA_FETCH_MAX_ATTEMPTS": "1",
            "PAA_FETCH_BASE_DELAY": "0",
        },
    )
    assert result.returncode == 0, result.stderr
    assert stub.pulls() == [
        "pull registry.example.com/library/postgres:16@sha256:" + _DIGEST
    ], stub.calls()
    assert stub.tags() == [
        "tag registry.example.com/library/postgres:16@sha256:" + _DIGEST + " postgres:16"
    ], stub.calls()


def test_the_tarball_list_does_not_leak_into_the_global_scope() -> None:
    """The array of tarballs belongs to the function that fills it.

    ``tarballs`` used to be a plain assignment inside ``load_images``, so it
    was a global: it outlived the call, and a second call in the same shell
    would find a stale value waiting for it. The script exits after a load
    today, so the leak is not observable in the current flow — which is
    exactly why it needs a lock rather than a report about a bug that
    happens to be harmless today.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    assert re.search(r"^\s*local -a tarballs", source, re.MULTILINE), (
        "the tarball list is a global — it outlives the function that fills it"
    )


def test_the_script_never_touches_a_running_container(tmp_path: Path) -> None:
    """The script operates on the local image store, and nothing else.

    It inspects, pulls, tags and loads. It does not stop, remove, restart or
    prune anything — least of all a container that is carrying somebody's
    work.

    The assertion is on the ``(verb, subcommand)`` **pair**, and the allowed
    set is asserted as EXACTLY that set. A first-word check cannot see
    ``docker image prune -f`` or ``docker image rm``, because both of them
    start with "image" — and the first of those is the command that would
    delete somebody else's images.
    """
    stub = _Stub(tmp_path)
    path = f"{_tool_path(tmp_path)}:{stub.bin}"
    # A fetch under a mirror, so that every command the script may issue is
    # exercised: inspect, pull, tag and info.
    _run(
        [],
        path=path,
        env={
            **stub.env("pulled"),
            "PAA_IMAGE_MIRROR": "registry.example.com",
            "PAA_FETCH_MAX_ATTEMPTS": "1",
            "PAA_FETCH_BASE_DELAY": "0",
        },
    )
    # And a load, so that the fifth command is exercised too.
    images = tmp_path / "images"
    images.mkdir()
    (images / "one.tar").write_bytes(b"not a real tarball")
    _run(
        ["--load", str(images)],
        path=path,
        env=stub.env("absent"),
    )
    calls = stub.calls()
    assert calls, "the script made no Docker call at all"
    commands = {_command_of(call) for call in calls}
    assert commands == _ALLOWED_COMMANDS, (
        f"the script used {sorted(commands - _ALLOWED_COMMANDS)}"
    )
    for call in calls:
        words = set(call.split())
        assert not words & _FORBIDDEN_WORDS, f"the script issued {call!r}"
