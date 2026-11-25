# ADR-0047: The host-git integrity gate — deny table over the repository's own config

## Status

Accepted

*Recorded 2026-11-25 by Wave 7-F task H-1 (the finish wave) over the Wave 7
tree. It supersedes the A1 regex gate landed in commit d004138 (W7), keeping
that gate's contract — refuse, never execute, name the key not the value —
and widening the mechanism from a closed regular expression to a classified
deny table with a default-refuse last-segment rule. ADR-0045 (coder work
tools) is unaffected; this decision covers the host-side seam those tools
stand on.*

## Context

The four host-side git tools (`git_status`, `git_diff`, `git_stage`,
`git_commit`) run git on the HOST, outside the sandbox, because the workspace
*is* the user's repository — the sandbox exists to contain the agent's
arbitrary code, not to contain git itself (ADR-0045). Git, however, will
execute programs named in the repository's own configuration during ordinary
operations: `core.fsmonitor` runs on the next `status`, a
`filter.<name>.clean` driver runs on the next `add`, `diff.<driver>.command`
runs on any diff. The agent's `bash` tool can write `.git/config` and
`.gitattributes` — the protected-path rule binds the fs tools, not bash — so
that configuration is attacker-writable input, and a program it names would
execute with the host's privileges. Under a containment backend that is a
container escape; under the default subprocess backend the mechanism is
identical even where the privileges are not new.

The A1 gate closed the three keys a first review found (`core.fsmonitor`,
`filter.*.clean/smudge/process`, `diff.external`) with one regular
expression. A second review (the one that produced this ADR) asked the
adversarial question the first one skipped: *what else in git's config
vocabulary names a program, and what happens when git grows a new one?* The
regex had no answer for `core.hookspath`, `credential.<helper>`,
`alias.*`, `include.path` indirection, `merge.<driver>.driver`, `gpg.program`,
`core.sshcommand`, `url.<x>.insteadOf` rewriting, or `core.attributesfile`
redirecting the attributes scan — and no answer at all for a key git adds in
a future version.

## Decision

1. **Refuse, never execute.** Before any host-side git call, the
   repository's *local* config is probed read-only
   (`git config --local --list -z --no-includes`) and classified against a
   deny table in `src/paa/coder/git_integrity.py`. Any match raises
   `PaaError` naming the **key, never the value** — a value can carry
   anything the adversary wrote, and an error payload that quotes it becomes
   a reflection channel into the model's context. The refusal is bounded
   (five keys, then a count) whatever the adversary wrote.

2. **Four denial classes, one classifier.** `classify_config_key` maps a key
   to: *program-running* (exact keys — `core.fsmonitor`, `core.hookspath`,
   `core.sshcommand`, `core.editor`, `core.pager`, `core.askpass`,
   `gpg.program`, `diff.external`, `core.attributesfile` — plus any key
   whose last dot-segment is `command`, `driver`, `helper`, `program`,
   `textconv`, `clean`, `smudge`, or `process`); *indirection*
   (`credential.*`, `alias.*`, `include.*`, `includeIf.*`, `protocol.*`
   wholesale, and `url.<anything>.insteadOf`/`pushInsteadOf`). Matching is
   case-insensitive, as git's own section and key vocabulary is.

3. **Default-refuse for the future.** The last-segment rule is the decision
   that ages: a program-naming key git adds in a later version is denied by
   default rather than waiting for a table update. Every real git key ending
   in one of those segments names something executable or loadable; the
   benign false positives that class could catch (`protocol.version`) are
   already inside a wholesale-denied prefix, and the tool surface has no
   network git operations, so nothing functional is lost.

4. **Attributes too.** The root attributes files (`.gitattributes` and
   `.git/info/attributes`) are scanned for `filter=` selections. A
   subdirectory `.gitattributes` can only *select* a driver; the driver's
   program is defined in config, which the deny table refuses — so the root
   scan is defence in depth, not the only barrier.

5. **`--local` only, operator's scope respected.** The probe reads
   repo-local config alone. The host operator's own global filters
   (git-lfs and friends) are theirs, outside what the workspace can write;
   refusing them would break every lfs checkout for no security gain.

6. **Defence in depth behind the gate.** Every `_run_git` invocation now
   carries `-c core.fsmonitor=false` (inserted in code, where the model
   cannot reach it) and the child environment forces `GIT_CONFIG_NOSYSTEM=1`
   — the gate refuses repo-local indirection; the forced flag keeps
   fsmonitor dead even in the window between probe and call, and the env
   keeps the host's `/etc/gitconfig` out of every host-side call.

7. **Probe failure is a refusal.** An exit code other than 0/1 (0 = listed,
   possibly empty; 1 = no matches) means a config git cannot even read —
   including a deleted `.git/config` — which is itself a workspace that
   cannot be shown safe, and is refused.

## Consequences

Costs: every host-side git call now runs one extra probe process (the
`config --list` read); a repository that legitimately wants a local filter
driver cannot use the host-side tools until the config is removed (the
refusal message says exactly that); and the last-segment rule is a blunt
instrument that will deny a hypothetical future *benign* key ending in
`.command` — a false positive that fails safe and is resolved by moving the
key to global scope, which is where operator-owned configuration belongs
anyway.

The honesty line, stated rather than implied: a hostile repository *cloned
by a human* remains a hostile input to any git client — this gate protects
the runtime from the workspace, not the human from the internet. Hooks are
already dead by a different mechanism (`-c core.hooksPath=` forced empty on
commit), and the probe itself executes nothing (`--list` reads, `-z` makes
the output machine-splittable, `--no-includes` stops `include.*`
indirection from being followed while the key itself is denied).

To move back: delete the `repo_config_gate` call from
`git_tools._preflight` and the module — but moving back reopens the
container-escape class this ADR exists to close, so the realistic reversion
path is narrowing the table, not removing the gate.
