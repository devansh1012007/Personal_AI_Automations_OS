# Docker daemon configuration for gVisor

`daemon.json` is the reference configuration for running PAA's sandbox worker
containers under [gVisor](https://gvisor.dev) (`runsc`).

## Why this file has no comments in it

`daemon.json` is **strictly JSON with no comment syntax**, and the Docker
daemon rejects any top-level key it does not recognise:

```
$ dockerd --validate --config-file deploy/docker/daemon.json
unable to configure the Docker daemon with file deploy/docker/daemon.json:
the following directives don't match any configuration option: somekey
```

The daemon does not ignore an unknown key — it **refuses to start**. So this
file carries no comments, and the explanation lives here, in this directory,
where it cannot break the host's Docker. The same reasoning applies to the
`runtimeArgs` below: they are *values*, and they are load-bearing.

`tests/deployment/test_daemon_config.py` asserts that every top-level key in
this file is one the daemon actually knows, so a future edit cannot
reintroduce the failure.

## Installing it

```bash
# 1. install gVisor (the official static release)
curl -fsSLO https://github.com/google/gvisor/releases/download/release-20261012.0/SHA256SUMS
curl -fsSLO https://github.com/google/gvisor/releases/download/release-20261012.0/gvisor-x86_64.tar.bz2
sha256sum -c --ignore-missing SHA256SUMS
tar -xjf gvisor-x86_64.tar.bz2
sudo install -m 0755 runsc /usr/local/bin/runsc

# 2. install it: copy, and merge by hand if the host already has a
#    daemon configuration (a registry mirror, a log driver, a storage
#    driver). This command REPLACES the file.
sudo cp deploy/docker/daemon.json /etc/docker/daemon.json

# 3. restart and verify
sudo systemctl restart docker
docker info --format '{{json .Runtimes}}'    # must list "runsc"
docker run --rm --runtime=runsc hello-world
```

Step 2 **overwrites** `/etc/docker/daemon.json`. If the host already has a
daemon configuration — a registry mirror, a log driver, a storage driver —
merge this file into it instead of replacing it, or the host loses those
settings along with the daemon.

## What the `runtimeArgs` are for

gVisor does **not** let a container open a host Unix socket merely because the
socket was bind-mounted into it. The socket has to be named explicitly in the
runtime's `runtimeArgs`:

```json
"runtimeArgs": [
  "--hostuds=/var/run/docker.sock",
  "--hostuds=/run/docker.sock"
]
```

Without them, the `docker` client inside the `paa` container fails with
`permission denied while trying to connect to the docker API` — an error that
reads like a socket-permission problem and not like a gVisor problem, and that
makes the whole gVisor path look half-working. This is the single most likely
way to get the deployment wrong.

**Both** socket spellings are listed because `/var/run` is a symlink to `/run`
on the host but not necessarily inside the container, and gVisor matches the
`hostuds` path against the mount namespace it is given. The same list appears
in `docker-compose.gvisor.yml` and `docs/deployment/docker.md` — keep the
three in step.

## The path

`/usr/local/bin/runsc` is the conventional location for the official static
release. If you install it elsewhere, change the `path` here to match — a
`path` that does not exist makes the daemon refuse to start.
