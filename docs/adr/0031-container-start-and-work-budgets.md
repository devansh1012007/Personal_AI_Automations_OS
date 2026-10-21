# ADR-0031: The container's start is not charged to the work

## Status

Accepted

## Context

`SandboxSpec.timeout_seconds` is the wall-clock budget on a step's **work**, and
`SandboxSettings.container_start_grace_seconds` was introduced (in commit `1c9ffa3`)
as the time "charged to the container's start, over and above the step's own timeout".

The two were **added together** and the sum was handed to a single timer:

```python
budget = spec.timeout_seconds + self._settings.container_start_grace_seconds
stdout_raw, stderr_raw = await asyncio.wait_for(proc.communicate(), timeout=budget)
```

That was not a stylistic choice. `docker run` is **one opaque call**: it creates the
container, starts it, attaches to it, waits for it and tears it down, all inside the
single `proc.communicate()`. The backend therefore could not observe where the start
ended and the work began, and a budget it cannot see a boundary in, it cannot
separate. Adding was the only thing left.

The consequence is that the *workload* could run for `timeout_seconds + grace`. The
defect was measured on this host (2026-10-21) against the repository's own image
(`paa/base_worker:v4.1`), with `timeout_seconds=0.25`:

| what | measured |
|---|---|
| the wall clock of a three-second workload | **3.6 s** |
| the budget the caller set | 0.25 s |
| the budget the caller got | 5.25 s (`0.25 + 5.0`) |
| `timed_out` | `False` |
| the exit code | `0` |

A quarter-second budget became five and a quarter seconds of work, reported as a
success. The budget the caller set was not the budget the caller got, and nothing in
the result said so.

The three costs the platform actually has, measured the same day on **two
hosts of different load** — the busier one the plan was measured on, and the
quieter one this ADR was written on. They are the same phases measured
twice, and the difference is the daemon's load rather than a disagreement
about what was measured: `docker start` is the phase that *waits*, for the
runtime to report the container running, so it is the phase the load shows
up in, while the container's *fixed* cost — which lives inside the
container's own lifetime — moves far less:

| cost | measured — the busier host (the plan's) | measured — the quieter host (this ADR's) |
|---|---|---|
| `docker create` | 165–205 ms | 161–194 ms |
| `docker start` (returns once the container's process is running) | 530–740 ms | 202–229 ms |
| the container's **fixed** cost — the runtime's setup and teardown, inside the lifetime, before and after the workload's own execution | **186–225 ms** for `sh -c true` | **114–130 ms** for `sh -c true` |
| the container's lifetime for `python -c 'time.sleep(3)'` | 3212–3226 ms | 3142 ms — three seconds of work plus the fixed cost |

**A quantity that moves with the host's load is quoted as a range, and the
host is named.** The plan's figures and this ADR's are the same
measurements under two conditions, not two claims about the same thing; a
reader quoting one number for `docker start` has to say which host it came
from. The consequences below use the **busier** host's start (530–740 ms),
because the argument they support is about a *loaded* daemon — the case the
start grace exists for — and the busier host's fixed cost (~200 ms), because
that is the figure the modality profile's budget is sized against.

And `docker logs` **demultiplexes**: a container writing to both streams comes back
with `MARKER_OUT` on stdout and `MARKER_ERR` on stderr, exactly as `docker run`
does.

## Decision

**The mechanism changes so the boundary is observable, and the two budgets are
enforced independently.**

```
docker create  →  docker start  →  docker inspect  →  docker wait
                →  docker logs  →  docker inspect  →  docker rm -f
```

- `docker start` returns once the container's process is running, so the end of the
  start is a fact and not an assumption.
- `docker inspect` reports `State.StartedAt` — the **daemon's** clock, which on this
  host is the host's clock — and later `State.FinishedAt`, `State.ExitCode` and
  `State.OOMKilled`.
- `docker wait` returns the exit code and blocks until the container stops.
- `docker logs` demultiplexes, so the stream separation survives.
- `docker rm -f` replaces `--rm`, in a `finally`, because the logs have to still be
  there after the container exits. A container that is never removed is precisely the
  orphan the timeout exists to prevent.

| budget | bounds | enforced by | reported as |
|---|---|---|---|
| `container_start_grace_seconds` | the `docker create` + `docker start` calls | a `wait_for` around each call | `start_duration_ms` |
| `timeout_seconds` | the container's **lifetime** (`StartedAt` → `FinishedAt`) | the kill at the work deadline | the duration in the result |

**The work deadline is `StartedAt + timeout_seconds`, in the daemon's clock.** A
deadline computed from the client's own clock would drift by however late the client
was, and a deadline that drifts is a deadline the caller cannot rely on. The client's
clock is the fallback when the daemon does not report a start time, and the budget is
capped at the caller's own `timeout_seconds` either way: the deadline is honoured when
the clocks agree, and the caller's ask is the ceiling when they do not.

**The fallback is anchored on the client's own start-of-work clock** — the moment
`docker start` returned, which is the client's closest observation of the container's
start. It is deliberately *not* anchored on the moment of the *inspect*: an anchor that
late would measure the caller's budget from the report rather than from the work, and
the container's total lifetime would be `timeout_seconds` plus the whole of the start
and the inspect — more than the caller asked for, which is the very thing the fallback
exists to avoid. The fallback fires only when the daemon does not answer or reports
the zero time, so the excess is at most one round trip.

**A daemon whose clock runs behind the host's is named, not hidden.** The deadline is
in the daemon's clock and the budget is measured against the client's `now`, so a
daemon behind by more than `timeout_seconds` makes the budget zero and the container
is killed the instant the wait starts. That errs the safe way, but reported as an
ordinary work timeout it would blame a workload that did nothing wrong — so the
backend logs a warning that **names the clock** and reports the measured lag between
the daemon's `StartedAt` and the client's own start-of-work clock.

When the wait exceeds the deadline the container is killed with `docker kill` — the
same path the old code used, because killing the *client* leaves the container running
and holding the workspace bind mount.

**The container's fixed cost is charged to the caller, and that is deliberate.** The
lifetime includes the runtime's setup and teardown — ~200 ms, measured (186–225 ms
on the busier of the two hosts above, 114–130 ms on the quieter one) — which is
*before* and *after* the workload's own execution. The workload's own duration is not
separately observable: the daemon reports the lifetime, and the budget bounds that.
The subprocess backend charges its own ~5 ms `fork` to the caller today; the
container backend charging its ~200 ms setup is the same rule at a different scale.

**`duration_ms` keeps its meaning** — the total wall clock, and the sandbox's own
measurement of the two phases together. The ranking signal is a **different** field
with the same name: the improvement loop (`improvement/optimizer.py`) reads
`duration_ms` from the *run record* (`hot_serving_execution_runs`), which
`observability/runs.py` computes from the run's own `started_at`/`ended_at` and
never from `SandboxResult.duration_ms`. The two only share a name, so redefining
*this* field would not change a ranking signal — it is left alone because a total
that silently stops counting the start is not a total. The start is reported
separately, in the new `start_duration_ms` field on `SandboxResult`.

**`killed_reason` gains `"start_timeout"`.** `"timeout"` means the *work* overran;
`"start_timeout"` means the *start* overran the grace. They demand opposite
responses — the first is a workload problem, the second a **host** problem (a daemon
that is too slow, a missing image, a runtime that is not registered) — and the old
result could not tell them apart. The interaction with the memory detection
(`exit_code == 137 and not timed_out ⟹ "memory"`) is ordering-sensitive: a container
killed by the start grace also exits 137, so the start timeout is checked **first**.

**The other backends are honest about the setting.** The subprocess and WSL backends
have no container start phase — a `fork`/`exec` is milliseconds, and the WSL2 VM's
start happens in the (cached) health check, not per run. They **ignore**
`container_start_grace_seconds` entirely rather than adding it to something, and say
so in their own report — the subprocess backend in both its `preflight` and its
`describe()`, the WSL backend in its `describe()`. A setting that only one backend
honours must not be silently applied to the others.

**The modality profiles' budgets are raised — the one policy change here.** A SIMPLE
step's budget was 0.25 s, and the container's fixed cost is ~200 ms, so a 0.25 s
budget cannot accommodate even a trivial container step, and a 0.25 s *workload*
needs a ~450 ms lifetime. **SIMPLE goes from 0.25 s to 1.0 s** — five times the
measured fixed cost. STANDARD (10 s), COMPLEX (30 s) and MAX (unbounded) are
unchanged.

## Consequences

**The trade-off, stated plainly.** A SIMPLE step's blast radius grows from 0.25 s of
container time to 1.0 s. The *wall clock* barely changes, because the container's
start — **530–740 ms** on the busier of the two hosts above, **202–229 ms** on the
quieter one — is most of what the old quarter-second step spent, and that start is
no longer charged to the work. The alternative — leaving the profiles alone — makes
every SIMPLE step fail on the container backend, because the fixed cost alone exceeds
the budget. This is the one consequence here that deserved a deliberate decision
rather than a default, and it is recorded in `core/types.py` next to the profile it
changes.

**The before and after, measured.** The same three-second workload with the same
0.25-second budget, on the same host: **3.6 s** and `timed_out=False` before the
change, **0.85 s** and `timed_out=True` after it, with no container left behind.
Those two numbers are the defect and its repair, and they are quoted as a pair
because they were measured as one — the same host, the same workload, the same
budget.

Both of those numbers are about the RFC's **intended** 0.25 s; against the
**effective** old behaviour the change is a *shrink*, not a growth: the old additive
budget gave a SIMPLE step `0.25 + 5.0 = 5.25` seconds of work, so a SIMPLE workload
that ran for two seconds was allowed and now times out at 1.0 s. The old budget was
the defect, and a workload that needed more than 1.0 s was never what the profile
meant.

**A state query that cannot be made does not destroy the work.** `docker inspect` is
*reporting*, and a report that cannot be made must not destroy the work it was
reporting on: a daemon that does not answer inside the call's budget degrades exactly
as a daemon that cannot be reached does — the run continues, and the deadline falls
back to the client's own clock. This matters because the pre-work inspect's budget
**is** the start grace, so an operator who tightens the grace to fail fast on a slow
host *raises* the probability of hitting it. (An earlier version let the
`TimeoutError` escape: the container was created, started, killed and removed because
the *state query* did not answer, and no result was produced at all — a bare
`TimeoutError`, which is not a `SandboxError` and so is not on the failure surface the
callers handle.)

**`WslSandbox.describe()` gained a `distro` key.** It is outside this change's scope,
and it is recorded here rather than left unmentioned: the key names the distribution
the backend will run in, which is the one fact about a WSL run that the identity
fields do not carry, and `describe()` is the ledger's and the doctor's view of the
backend.

**A container that cannot be created is reported as the daemon's own error.** The
exit code and the daemon's message are passed through verbatim, because "Unable to
find image" names the problem and "the step failed" does not.

**`docker rm -f` replaces `--rm`, and the removal is in a `finally`.** Every path out
of the lifecycle — the success, the work timeout, the start breach, and an exception
raised in the middle — removes the container. The logs are read *before* the removal,
because the removal destroys the container's filesystem layer and the logs are the
only record of what it did.

**The `docker` CLI is now used for a container's whole life.** The daemon is asked
for the state rather than inferred from a client's exit code, so the exit code in the
result is the **container's** exit code, not the CLI's.

**What it would take to move back.** A single opaque `docker run` and one timer. The
boundary between the start and the work would have to become unobservable again, and
the budget the caller sets would have to mean something other than what it says.
