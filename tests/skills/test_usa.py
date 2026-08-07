"""UnifiedSkillAdapter: the RFC §8.2 state machine, end to end.

Covers the native fast path through discovery -> authorization -> invoke ->
secret proxy -> output validation -> reliability adjustment, with a real
SkillRegistry (the ``db`` fixture) and a real SecretBroker.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import structlog

from paa.core.errors import PermissionDeniedError, SkillContractError
from paa.core.types import Permission, PermissionMode
from paa.skills.adapters.native import NativeAdapter, NativeCallable
from paa.skills.contracts import SkillContract, SkillInvocation
from paa.skills.registry import SkillRegistry
from paa.skills.secrets import SecretBroker
from paa.skills.usa import UnifiedSkillAdapter
from paa.storage.relational.database import Database

_SECRET_NAME = "API_KEY"
_SECRET_VALUE = "s3cr3t-token-do-not-log"


def _native_contract(
    name: str,
    *,
    output_schema: dict[str, Any] | None = None,
    permissions: tuple[str, ...] = (),
    reliability: float = 0.5,
) -> SkillContract:
    return SkillContract(
        skill_name=name,
        provider="native",
        version="1.0.0",
        description=f"A native skill called {name} used to exercise the state machine.",
        input_schema={"type": "object"},
        output_schema=output_schema or {"type": "object"},
        risk_profile=0.2,
        required_permissions=permissions,  # type: ignore[arg-type]
        reliability_weight=reliability,
        invocation=SkillInvocation(kind="native_callable", target=name),
    )


async def _register(
    registry: SkillRegistry,
    adapter: NativeAdapter,
    contract: SkillContract,
    fn: NativeCallable,
) -> None:
    await registry.register(contract)
    adapter.register(contract, fn)


@pytest.fixture
def broker(tmp_path: Path) -> SecretBroker:
    b = SecretBroker(tmp_path / "secrets.json", passphrase="unit-test-passphrase")
    b.put(_SECRET_NAME, _SECRET_VALUE)
    return b


@pytest.fixture
def wired(db: Database) -> tuple[UnifiedSkillAdapter, SkillRegistry, NativeAdapter]:
    registry = SkillRegistry(db)
    native = NativeAdapter()
    usa = UnifiedSkillAdapter(registry, [native])
    return usa, registry, native


class TestEndToEnd:
    async def test_success_raises_reliability(self, wired) -> None:
        usa, registry, native = wired
        contract = _native_contract(
            "good-skill",
            output_schema={"type": "object", "properties": {"result": {"type": "string"}},
                           "required": ["result"]},
        )
        await _register(registry, native, contract, lambda args: {"result": "done"})

        result = await usa.dispatch("good-skill", {}, mode=PermissionMode.AUTO)
        assert result.ok
        assert result.output_valid
        assert result.output == {"result": "done"}
        assert result.adapter == "native"

        stored = await registry.get("good-skill")
        assert stored is not None
        assert stored.reliability_weight == pytest.approx(0.55)  # 0.5 + up

    async def test_malformed_output_lowers_reliability(self, wired) -> None:
        usa, registry, native = wired
        contract = _native_contract(
            "sloppy-skill",
            output_schema={"type": "object", "properties": {"result": {"type": "string"}},
                           "required": ["result"]},
        )
        # Returns an int where the schema demands a string.
        await _register(registry, native, contract, lambda args: {"result": 123})

        result = await usa.dispatch("sloppy-skill", {}, mode=PermissionMode.AUTO)
        assert result.ok  # the mechanism ran...
        assert result.output_valid is False  # ...but its output is not believable
        assert result.schema_errors

        stored = await registry.get("sloppy-skill")
        assert stored is not None
        assert stored.reliability_weight == pytest.approx(0.40)  # 0.5 - down

    async def test_crash_lowers_reliability_and_returns(self, wired) -> None:
        usa, registry, native = wired
        contract = _native_contract("boom-skill")

        def _boom(args: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("kaboom")

        await _register(registry, native, contract, _boom)
        result = await usa.dispatch("boom-skill", {}, mode=PermissionMode.AUTO)
        assert result.ok is False
        assert result.output_valid is False
        stored = await registry.get("boom-skill")
        assert stored is not None
        assert stored.reliability_weight == pytest.approx(0.40)

    async def test_unknown_skill_raises(self, wired) -> None:
        usa, _registry, _native = wired
        with pytest.raises(SkillContractError, match="unknown or inactive"):
            await usa.dispatch("ghost", {}, mode=PermissionMode.AUTO)

    async def test_bad_arguments_raise_before_execution(self, wired) -> None:
        usa, registry, native = wired
        contract = _native_contract(
            "picky-skill",
            output_schema={"type": "object"},
        )
        contract = contract.model_copy(
            update={"input_schema": {"type": "object", "required": ["n"]}}
        )
        calls: list[dict[str, Any]] = []

        def _record(args: dict[str, Any]) -> dict[str, Any]:
            calls.append(args)
            return {}

        await _register(registry, native, contract, _record)
        with pytest.raises(SkillContractError, match="input_schema"):
            await usa.dispatch("picky-skill", {}, mode=PermissionMode.AUTO)
        assert calls == []  # never executed


class TestSecurityAuthorization:
    async def test_permission_exceeding_mode_is_hard_stop(self, wired) -> None:
        usa, registry, native = wired
        contract = _native_contract(
            "egress-skill", permissions=(Permission.NET_EGRESS.value,)
        )
        await _register(registry, native, contract, lambda args: {})

        with pytest.raises(PermissionDeniedError) as exc:
            await usa.dispatch("egress-skill", {}, mode=PermissionMode.LOCKDOWN)
        assert Permission.NET_EGRESS.value in exc.value.missing

    async def test_permission_granted_in_mode_proceeds(self, wired) -> None:
        usa, registry, native = wired
        contract = _native_contract(
            "egress-skill", permissions=(Permission.NET_EGRESS.value,)
        )
        await _register(registry, native, contract, lambda args: {})
        result = await usa.dispatch("egress-skill", {}, mode=PermissionMode.AUTO)
        assert result.ok


class TestSecretProxy:
    async def test_secret_reaches_skill_when_granted(self, wired, broker: SecretBroker) -> None:
        usa, registry, native = wired
        contract = _native_contract(
            "reader-skill",
            output_schema={"type": "object", "properties": {"matched": {"type": "boolean"}}},
            permissions=(Permission.SECRET_READ.value,),
        )

        def _reader(args: dict[str, Any], *, get_secret: Any) -> dict[str, Any]:
            secret = get_secret(_SECRET_NAME)
            # Prove the real value was obtained without ever returning it.
            return {"matched": secret.reveal() == _SECRET_VALUE}

        await _register(registry, native, contract, _reader)
        result = await usa.dispatch(
            "reader-skill", {}, mode=PermissionMode.AUTO, secret_broker=broker
        )
        assert result.ok
        assert result.output == {"matched": True}

    async def test_secret_skill_refused_without_permission(
        self, wired, broker: SecretBroker
    ) -> None:
        usa, registry, native = wired
        contract = _native_contract(
            "reader-skill", permissions=(Permission.SECRET_READ.value,)
        )
        await _register(registry, native, contract, lambda args, *, get_secret: {})
        # LOCKDOWN does not grant SECRET_READ -> refused before the sandbox boots.
        with pytest.raises(PermissionDeniedError):
            await usa.dispatch(
                "reader-skill", {}, mode=PermissionMode.LOCKDOWN, secret_broker=broker
            )

    async def test_secret_value_never_appears_in_logs(
        self, wired, broker: SecretBroker
    ) -> None:
        usa, registry, native = wired
        contract = _native_contract(
            "reader-skill",
            output_schema={"type": "object", "properties": {"matched": {"type": "boolean"}}},
            permissions=(Permission.SECRET_READ.value,),
        )

        def _reader(args: dict[str, Any], *, get_secret: Any) -> dict[str, Any]:
            secret = get_secret(_SECRET_NAME)
            return {"matched": secret.reveal() == _SECRET_VALUE}

        await _register(registry, native, contract, _reader)

        with structlog.testing.capture_logs() as captured:
            result = await usa.dispatch(
                "reader-skill", {}, mode=PermissionMode.AUTO, secret_broker=broker
            )
        assert result.ok
        blob = repr(captured)
        assert _SECRET_VALUE not in blob
        # The audit trail records the access by name, proving it did happen.
        assert any(rec.secret_name == _SECRET_NAME and rec.granted for rec in broker.audit_log())

    async def test_secret_value_never_in_exception_text(
        self, wired, broker: SecretBroker
    ) -> None:
        usa, registry, native = wired
        contract = _native_contract(
            "reader-skill", permissions=(Permission.SECRET_READ.value,)
        )
        await _register(registry, native, contract, lambda args, *, get_secret: {})
        try:
            await usa.dispatch(
                "reader-skill", {}, mode=PermissionMode.LOCKDOWN, secret_broker=broker
            )
        except PermissionDeniedError as exc:
            assert _SECRET_VALUE not in str(exc)
            assert _SECRET_VALUE not in repr(exc)
