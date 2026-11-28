"""Package installer — five gates between a downloaded package and a live skill.

A marketplace is the single largest attack surface in this design: it is the one
path by which code the user did not write becomes code the runtime will run. The
installer is therefore built as a sequence of gates, each of which can refuse,
and **a package that fails any gate leaves zero trace** in the registry — no
half-install, no dangling rows.

The gates, in order, and why the order matters:

1. **Signature** — is this really from who it claims? Cheapest check that can
   reject a wholesale forgery, so it runs first. Refused outright if invalid;
   ``unverified`` / ``untrusted_key`` require explicit opt-in.
2. **Content hash** — do the bytes match what was signed? Catches tampering
   between signing and install.
3. **AST scan** — does the code contain forbidden constructs? Runs the same
   deterministic scanner the critic uses (RFC §13), before any code executes.
4. **Permission check** — does the package demand more than the active mode
   grants? A package needing NET_EGRESS under LOCKDOWN is refused.
5. **Sandbox smoke test** — does it even run? Executed in a sandbox so a
   malicious package cannot touch the host during its own install test.

Every gate **fails closed**. A gate whose dependency is missing — no scanner
wired, no sandbox configured, a sandbox that fails its own health check —
refuses the install with a reason naming the missing piece; it never reports
itself as passed. The one honest exception is the smoke gate for a package
that declares no entrypoint at all: there is nothing the gate could execute,
so the report records the gate as **not applicable** (``na_gates``), never as
passed — the same truth rule the ledger follows. Only gates that ran and
passed are listed in ``passed_gates``, and only after all applicable gates
pass is the skill registered.
"""

from __future__ import annotations

import enum
import json
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from paa.core.types import Permission, PermissionMode
from paa.marketplace.package import SkillPackage
from paa.marketplace.signing import SignatureState, signing_is_secure, verify
from paa.storage.relational.base import RelationalBackend

__all__ = ["InstallGate", "InstallOutcome", "InstallReport", "PackageInstaller"]

log = structlog.get_logger(__name__)

#: The smoke test's wall-clock budget. Generous for a cold interpreter start
#: (the child pays its own boot cost inside the budget) and short enough
#: that a hung entrypoint cannot stall an install by minutes.
_SMOKE_TIMEOUT_SECONDS = 20.0


class InstallGate(str, enum.Enum):
    SIGNATURE = "signature"
    CONTENT_HASH = "content_hash"
    AST_SCAN = "ast_scan"
    PERMISSIONS = "permissions"
    SMOKE_TEST = "smoke_test"


class InstallOutcome(str, enum.Enum):
    INSTALLED = "installed"
    REFUSED = "refused"


@dataclass(slots=True)
class InstallReport:
    outcome: InstallOutcome
    package_name: str
    passed_gates: list[InstallGate] = field(default_factory=list)
    #: Gates that did not apply to this package, with the reason each was not
    #: run. A gate that cannot apply is recorded here — never in
    #: ``passed_gates`` — so a report never claims a gate passed when it did
    #: not run.
    na_gates: list[tuple[InstallGate, str]] = field(default_factory=list)
    refused_at: InstallGate | None = None
    reason: str | None = None
    signature_state: SignatureState = SignatureState.UNVERIFIED

    @property
    def installed(self) -> bool:
        return self.outcome is InstallOutcome.INSTALLED


class PackageInstaller:
    """Runs the five gates and registers a package only if all pass."""

    def __init__(
        self,
        db: RelationalBackend,
        *,
        scanner: Any = None,
        sandbox: Any = None,
        allow_unverified: bool = False,
    ) -> None:
        self._db = db
        self._scanner = scanner
        self._sandbox = sandbox
        self._allow_unverified = allow_unverified

    async def install(
        self,
        package: SkillPackage,
        *,
        mode: PermissionMode = PermissionMode.ASK,
    ) -> InstallReport:
        """Run every gate. Register only on a clean pass; leave no trace on refusal."""
        report = InstallReport(
            outcome=InstallOutcome.REFUSED, package_name=package.manifest.package_name
        )

        gate = await self._gate_signature(package, report)
        if gate is not None:
            return self._refuse(report, InstallGate.SIGNATURE, gate)
        report.passed_gates.append(InstallGate.SIGNATURE)

        if not package.verify_content_hash():
            return self._refuse(
                report, InstallGate.CONTENT_HASH, "content hash does not match manifest"
            )
        report.passed_gates.append(InstallGate.CONTENT_HASH)

        if (finding := self._gate_ast(package)) is not None:
            return self._refuse(report, InstallGate.AST_SCAN, finding)
        report.passed_gates.append(InstallGate.AST_SCAN)

        if (missing := self._gate_permissions(package, mode)) is not None:
            return self._refuse(report, InstallGate.PERMISSIONS, missing)
        report.passed_gates.append(InstallGate.PERMISSIONS)

        if not package.manifest.entrypoints:
            # The one gate that can be honestly not applicable: a package
            # with no entrypoint has nothing the smoke gate can execute. It
            # is recorded as not-run — never as passed — and the pipeline
            # flow's conformance stage is the execution proof for
            # pipeline-kind packages.
            report.na_gates.append(
                (InstallGate.SMOKE_TEST, "the package declares no entrypoint")
            )
        elif (smoke := await self._gate_smoke_test(package)) is not None:
            return self._refuse(report, InstallGate.SMOKE_TEST, smoke)
        else:
            report.passed_gates.append(InstallGate.SMOKE_TEST)

        await self._register(package, report)
        report.outcome = InstallOutcome.INSTALLED
        log.info("marketplace.installed", package=package.manifest.package_name)
        return report

    # -- gates -------------------------------------------------------------

    async def _gate_signature(self, package: SkillPackage, report: InstallReport) -> str | None:
        m = package.manifest
        if not m.signature or not m.publisher_key:
            report.signature_state = SignatureState.UNVERIFIED
            if self._allow_unverified:
                return None
            return "package is unsigned; set allow_unverified to install anyway"

        # Only a *secure* backend can authenticate a signature. Under the HMAC
        # dev fallback, a "valid" signature proves nothing (symmetric), so it is
        # treated as unverified rather than trusted.
        if not signing_is_secure():
            report.signature_state = SignatureState.UNVERIFIED
            if self._allow_unverified:
                return None
            return "signing backend is the insecure dev fallback; cannot authenticate"

        if not verify(m.signing_bytes(), m.signature, m.publisher_key):
            report.signature_state = SignatureState.INVALID
            return "signature is invalid — content or metadata was altered"

        if not await self._is_trusted_key(m.publisher_key):
            report.signature_state = SignatureState.UNTRUSTED_KEY
            if self._allow_unverified:
                return None
            return "signature is valid but the publisher key is not trusted"

        report.signature_state = SignatureState.VALID
        return None

    def _gate_ast(self, package: SkillPackage) -> str | None:
        if self._scanner is None:
            # Fail closed: a gate that cannot run refuses, it does not pass.
            # The scanner is a cheap in-process object, so "not wired" is a
            # caller's omission, and the reason names it as one.
            return (
                "the AST gate has no scanner — a gate that cannot run "
                "refuses (fail closed), it does not pass"
            )
        for name, source in package.python_sources().items():
            findings = self._scanner.scan(source, filename=name)
            hard = [f for f in findings if getattr(f, "severity", None) and _is_block(f)]
            if hard:
                return f"AST scan rejected {name}: {hard[0].message}"
        return None

    def _gate_permissions(self, package: SkillPackage, mode: PermissionMode) -> str | None:
        required: set[Permission] = set()
        for name in package.manifest.required_permissions:
            try:
                required.add(Permission(name))
            except ValueError:
                return f"package declares unknown permission {name!r}"
        missing = [p.value for p in required if not mode.grants(p)]
        if missing:
            return f"package requires permissions not granted in {mode.value}: {sorted(missing)}"
        return None

    async def _gate_smoke_test(self, package: SkillPackage) -> str | None:
        """Run the package's entrypoint in a sandbox. A crash here is a refusal,
        but it happens in isolation so it cannot harm the host.

        The run is the **production shape**, staged: the package's files are
        written to a throwaway directory (removed whether the gate passes
        or not, so a refused package leaves zero filesystem trace), the
        entrypoint is executed under the sandbox with the skill directory
        mounted read-only — the same mount contract
        :class:`~paa.skills.adapters.claw_hub.ClawHubAdapter.invoke` runs
        under — with an empty probe argument file passed in ``argv``, and
        the pass bar is the skill contract's own: exit 0 and a single JSON
        object recoverable from stdout. A package that cannot run its own
        entrypoint under containment is not installed, and one that runs
        but emits no JSON has already failed the contract its activation
        would advertise.
        """
        if self._sandbox is None:
            # Fail closed, the same rule as the AST gate: "no sandbox wired"
            # is a caller's omission, and the gate that cannot run refuses
            # rather than report itself as passed.
            return (
                "the smoke gate has no sandbox — a gate that cannot run "
                "refuses (fail closed), it does not pass"
            )
        try:
            healthy = await self._sandbox.healthcheck()
        except Exception:
            healthy = False
        if not healthy:
            # A sandbox that cannot pass its own health check cannot vouch
            # for a package's behaviour, so the gate refuses: an install that
            # claimed a smoke pass on an unhealthy sandbox would be exactly
            # the silent pass this gate exists to prevent.
            return (
                "the sandbox failed its health check — the smoke gate refuses "
                "(fail closed), it does not pass"
            )

        entrypoint = package.manifest.entrypoints[0]
        if entrypoint not in package.files:
            return f"declared entrypoint {entrypoint!r} is not in the package"

        with tempfile.TemporaryDirectory(prefix="paa-smoke-") as scratch:
            stage = Path(scratch) / "skill"
            stage.mkdir()
            for name, data in package.files.items():
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            args_file = Path(scratch) / "arguments.json"
            args_file.write_text(json.dumps({}), encoding="utf-8")
            from paa.sandbox.base import SandboxSpec

            spec = SandboxSpec(
                command=(sys.executable, str(stage / entrypoint), "--paa-args", str(args_file)),
                workspace_path=Path(scratch),
                read_only_mounts=(stage.resolve(),),
                timeout_seconds=_SMOKE_TIMEOUT_SECONDS,
            )
            try:
                result = await self._sandbox.run(spec)
            except Exception as exc:
                return f"smoke test could not run under the sandbox: {type(exc).__name__}"

        if not result.ok:
            reason = result.killed_reason or ("timeout" if result.timed_out else "nonzero exit")
            return f"smoke test failed ({reason})"
        if _smoke_json_object(result.stdout) is None:
            return "smoke test produced no JSON object on stdout"
        return None

    # -- registration ------------------------------------------------------

    async def _register(self, package: SkillPackage, report: InstallReport) -> None:
        from paa.storage.relational.database import dumps, to_iso, utc_now

        m = package.manifest
        now = to_iso(utc_now())
        await self._db.execute(
            "INSERT INTO marketplace_packages "
            "(id, package_name, version, kind, publisher, publisher_key, description, "
            " manifest, content_hash, signature, signature_state, installed_at, trust_score) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(package_name, version) DO UPDATE SET "
            "  installed_at = excluded.installed_at, "
            "  signature_state = excluded.signature_state",
            (
                str(uuid.uuid4()), m.package_name, m.version, m.kind, m.publisher,
                m.publisher_key, m.description, dumps(m.as_dict()), m.content_hash,
                m.signature, report.signature_state.value, now,
                1.0 if report.signature_state is SignatureState.VALID else 0.0,
            ),
        )

    async def uninstall(self, package_name: str, version: str) -> bool:
        """Fully reverse an install. Returns whether a row was removed."""
        removed = await self._db.execute(
            "DELETE FROM marketplace_packages WHERE package_name = ? AND version = ?",
            (package_name, version),
        )
        return removed > 0

    # -- trust store -------------------------------------------------------

    async def trust_key(self, key_id: str, public_key: str, owner: str) -> None:
        from paa.storage.relational.database import to_iso, utc_now

        await self._db.execute(
            "INSERT INTO marketplace_trusted_keys "
            "(key_id, public_key, owner, added_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key_id) DO UPDATE SET "
            "public_key = excluded.public_key, owner = excluded.owner, "
            "added_at = excluded.added_at",
            (key_id, public_key, owner, to_iso(utc_now())),
        )

    async def _is_trusted_key(self, public_key: str) -> bool:
        row = await self._db.fetch_one(
            "SELECT 1 FROM marketplace_trusted_keys "
            "WHERE public_key = ? AND revoked_at IS NULL",
            (public_key,),
        )
        return row is not None

    def _refuse(self, report: InstallReport, gate: InstallGate, reason: str) -> InstallReport:
        report.outcome = InstallOutcome.REFUSED
        report.refused_at = gate
        report.reason = reason
        log.warning(
            "marketplace.refused", package=report.package_name, gate=gate.value, reason=reason
        )
        return report


def _is_block(finding: Any) -> bool:
    """Whether a scanner finding is severe enough to refuse the package."""
    sev = getattr(finding, "severity", None)
    name = getattr(sev, "value", str(sev)).lower() if sev is not None else ""
    return name in ("block", "critical", "high", "error")


def _smoke_json_object(stdout: str) -> dict[str, Any] | None:
    """The JSON object a smoke run must leave on stdout, or ``None``.

    Same tolerance as the claw-hub adapter's output reader: the whole
    payload first, then the last non-empty line — a well-behaved skill
    may print diagnostics before its result line. Anything else is a
    failed smoke, not a parse error: the gate refuses, never raises.
    """
    text = stdout.strip()
    if not text:
        return None
    for candidate in (text, text.splitlines()[-1].strip()):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None
