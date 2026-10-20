"""``deploy/docker/daemon.json`` must be a file the Docker daemon accepts.

The daemon's configuration is **strictly JSON with no comment syntax**, and
the daemon does not ignore an unknown top-level key — it **refuses to start**:

    $ dockerd --validate --config-file deploy/docker/daemon.json
    unable to configure the Docker daemon with file deploy/docker/daemon.json:
    the following directives don't match any configuration option: _comment

That is not a cosmetic problem. The file's whole purpose is to be copied to
``/etc/docker/daemon.json`` on the host, and a daemon that will not start
takes the substrate this project runs on with it. So the file is checked here
rather than discovered by an operator at 2am.

The explanation that a comment key would have carried lives in
``deploy/docker/README.md``, which cannot break anything.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_DOCKER = REPO_ROOT / "deploy" / "docker"
DAEMON_JSON = DEPLOY_DOCKER / "daemon.json"

#: The top-level directives the Docker daemon understands. The list is not
#: exhaustive — it is the set a file like this one would plausibly use, and
#: the point is that an *unknown* key is refused. Anything not in this set
#: must not appear at the top level of the file.
_KNOWN_DIRECTIVES = {
    "runtimes",
    "registry-mirrors",
    "insecure-registries",
    "log-driver",
    "log-opts",
    "storage-driver",
    "data-root",
    "exec-root",
    "hosts",
    "dns",
    "debug",
    "labels",
    "live-restore",
    "default-runtime",
    "experimental",
    "features",
    "group",
    "iptables",
    "ip6tables",
    "bridge",
    "proxy",
    "no-new-privileges",
    "userland-proxy",
    "max-concurrent-downloads",
    "max-concurrent-uploads",
    "shutdown-timeout",
    "containerd",
    "runtime",
    "builder",
    "authorization-plugins",
    "plugin",
    "seccomp-profile",
    "userns-remap",
    "init",
    "default-ulimits",
}


def test_the_daemon_file_is_valid_json() -> None:
    """A syntax error stops the daemon exactly as a bad key does."""
    parsed = json.loads(DAEMON_JSON.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)


def test_every_top_level_directive_is_one_the_daemon_knows() -> None:
    """An unknown key does not get ignored — it stops the daemon.

    This is the regression guard for the comment key that used to be in this
    file. It was the most natural place to explain the ``runtimeArgs``, and
    it would have bricked the host's Docker the moment an operator followed
    the documented install procedure.
    """
    parsed = json.loads(DAEMON_JSON.read_text(encoding="utf-8"))
    unknown = sorted(set(parsed) - _KNOWN_DIRECTIVES)
    assert not unknown, (
        f"deploy/docker/daemon.json has top-level keys the Docker daemon does "
        f"not recognise: {unknown}. The daemon refuses to start on an unknown "
        "directive — it does not ignore it."
    )


def test_the_runsc_runtime_is_registered_with_both_socket_spellings() -> None:
    """The socket has to be named, or the gVisor path looks half-working.

    gVisor does not let a container open a host Unix socket merely because
    it was bind-mounted in. Both spellings are listed because ``/var/run`` is
    a symlink to ``/run`` on the host but not necessarily inside the
    container.
    """
    parsed = json.loads(DAEMON_JSON.read_text(encoding="utf-8"))
    runsc = parsed["runtimes"]["runsc"]
    assert runsc["path"].endswith("runsc")
    args = runsc["runtimeArgs"]
    assert "--hostuds=/var/run/docker.sock" in args
    assert "--hostuds=/run/docker.sock" in args


def test_the_hostuds_list_agrees_with_the_compose_override() -> None:
    """Three files carry the list; they have to carry the same one.

    The deployment guide, the compose override and this file all document
    the same registration. A list that differs between them is a list that
    is wrong in one of them, and the operator cannot tell which.
    """
    compose = (REPO_ROOT / "docker-compose.gvisor.yml").read_text(encoding="utf-8")
    guide = (REPO_ROOT / "docs" / "deployment" / "docker.md").read_text(encoding="utf-8")
    for text in (compose, guide):
        assert "--hostuds=/var/run/docker.sock" in text
        assert "--hostuds=/run/docker.sock" in text


@pytest.mark.parametrize("name", ["daemon.json", "README.md"])
def test_the_deploy_docker_directory_ships_both(name: str) -> None:
    """The explanation has to ship with the file it explains.

    Moving the prose out of the JSON is only safe if it lands somewhere
    that is actually distributed with it.
    """
    assert (DEPLOY_DOCKER / name).exists()

