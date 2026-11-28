"""The adapter registration surface — the seam the review asked for (W9A-REG).

The four promises the surface makes, each pinned with its reversal:

* **a third adapter joins in-process and dispatches** — a factory that is
  none of the three builtins, registered under its own name, is handed
  back by ``adapter_for`` and drives a real USA dispatch. Reversal: if the
  dispatch took the builtin ``NativeAdapter`` instead, the bound callable
  would not exist and the result would be ``ok=False`` with an empty
  stamp — the marker in the output and the recorded invocation are the
  proof the third-party class ran;
* **duplicate registration refuses loudly** — the second
  ``register_adapter`` under a taken name raises and the first factory
  survives. Reversal: a silent override would leave the shadowed adapter
  unreachable and the operator unwiser;
* **an unknown name names the known ones** — the refusal message carries
  every registered adapter, the ``resolve_context_provider`` shape;
* **entry-point discovery degrades per plugin** — a broken entry is warned
  and skipped while its siblings register, the order is sorted by name, an
  entry reaching for a builtin's name is a skip rather than a crash, and
  the lazy scan a miss triggers runs once per process.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

import pytest
import structlog.testing

import paa.skills.adapters.registry as registry_module
from paa.core.errors import PaaError
from paa.core.types import PermissionMode
from paa.skills.adapters.base import SecretProvider, SkillAdapter, SkillResult
from paa.skills.adapters.claw_hub import ClawHubAdapter
from paa.skills.adapters.native import NativeAdapter
from paa.skills.adapters.registry import (
    adapter_for,
    discover_entry_point_adapters,
    register_adapter,
)
from paa.skills.contracts import SkillContract, SkillInvocation
from paa.skills.registry import SkillRegistry
from paa.skills.usa import UnifiedSkillAdapter
from paa.storage.relational.database import Database

if TYPE_CHECKING:
    from paa.sandbox.base import Sandbox

#: The names the builtins must hold once the registry has been touched.
_BUILTIN_NAMES = ("claw_hub", "mcp_server", "native")


class _ThirdPartyAdapter(SkillAdapter):
    """A fourth modality the host never shipped: an in-process echo.

    Implements the adapter contract directly — extends no builtin — so the
    dispatch test proves the registry serves *any* implementation, and
    stamps every invocation with its marker so a result that took a
    builtin's path can be told apart from one that crossed this class.
    """

    def __init__(self, *, marker: str = "third-party") -> None:
        self._marker = marker
        self._callables: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}
        self.invoked: list[str] = []

    @property
    def provider(self) -> str:
        # The contract's provider vocabulary is closed (a Literal on
        # SkillContract); an in-process third-party modality rides "native".
        return "native"

    def bind(self, name: str, fn: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self._callables[name] = fn

    async def discover(self) -> list[SkillContract]:
        return []  # pragma: no cover - not the behaviour under test here

    async def invoke(
        self,
        contract: SkillContract,
        arguments: dict[str, Any],
        *,
        sandbox: Sandbox | None,
        timeout: float | None,  # noqa: ASYNC109 - the adapter API's own per-call budget
        secret_broker: SecretProvider | None = None,
    ) -> SkillResult:
        self.invoked.append(contract.skill_name)
        fn = self._callables.get(contract.skill_name)
        if fn is None:
            return SkillResult(
                ok=False, error=f"nothing bound under {contract.skill_name!r}"
            )
        return SkillResult(ok=True, output={**fn(arguments), "via": self._marker})


class _FakeEntryPoint:
    """An ``importlib.metadata.EntryPoint`` stand-in: a name and a loader."""

    def __init__(self, name: str, load: Callable[[], Any]) -> None:
        self.name = name
        self._load = load

    def load(self) -> Any:
        return self._load()


@pytest.fixture
def pristine_registry() -> Iterator[registry_module]:
    """A registry with no seed, no scan and no third-party names in it.

    The table is process-global — that is the point of a registry — so the
    tests here reset it before and after: a name registered in one test
    must not be resolvable in another. Clearing the flags on the way out is
    enough, because the next touch from anywhere re-seeds the builtins
    lazily, which is itself the behaviour under test.
    """
    registry_module._FACTORIES.clear()
    registry_module._seeded_builtins = False
    registry_module._default_group_scanned = False
    yield registry_module
    registry_module._FACTORIES.clear()
    registry_module._seeded_builtins = False
    registry_module._default_group_scanned = False


def _native_contract(name: str) -> SkillContract:
    return SkillContract(
        skill_name=name,
        provider="native",
        version="1.0.0",
        description=f"A native skill called {name} used to exercise the registry dispatch.",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        risk_profile=0.2,
        invocation=SkillInvocation(kind="native_callable", target=name),
    )


def _patch_entry_points(
    monkeypatch: pytest.MonkeyPatch, entries: list[_FakeEntryPoint]
) -> None:
    """Point ``importlib.metadata.entry_points`` at ``entries``.

    The registry resolves the call through the module attribute at call
    time, so patching the stdlib name is enough — no private binding of
    the registry's own has to be reached for.
    """

    def _fake(**params: Any) -> list[_FakeEntryPoint]:
        assert params.get("group") == registry_module.ADAPTER_ENTRY_POINT_GROUP, params
        return list(entries)

    monkeypatch.setattr(importlib.metadata, "entry_points", _fake)


class TestBuiltins:
    def test_first_use_registers_the_three_builtins(self, pristine_registry) -> None:
        assert registry_module._FACTORIES == {}  # nothing registered at import
        assert adapter_for("claw_hub") is ClawHubAdapter
        assert adapter_for("mcp_server") is registry_module.McpServerAdapter
        assert adapter_for("native") is NativeAdapter

    def test_the_factories_construct_with_their_own_shapes(
        self, pristine_registry, tmp_path
    ) -> None:
        """A registration holds the factory, not an instance: each modality's
        constructor keeps the signature it always had."""
        assert isinstance(adapter_for("claw_hub")(tmp_path), ClawHubAdapter)
        assert isinstance(adapter_for("native")(), NativeAdapter)
        assert isinstance(
            registry_module.McpServerAdapter(["python", "-m", "some.server"]),
            registry_module.McpServerAdapter,
        )


class TestThirdParty:
    async def test_a_third_adapter_registers_and_dispatches(
        self, pristine_registry, db: Database
    ) -> None:
        register_adapter("third-party-echo", _ThirdPartyAdapter)
        assert adapter_for("third-party-echo") is _ThirdPartyAdapter

        adapter = adapter_for("third-party-echo")(marker="w9a")
        contract = _native_contract("echo-skill")
        skills = SkillRegistry(db)
        await skills.register(contract)
        adapter.bind(contract.skill_name, lambda args: {"heard": args.get("say", "")})

        result = await UnifiedSkillAdapter(skills, [adapter]).dispatch(
            "echo-skill", {"say": "hello"}, mode=PermissionMode.AUTO
        )

        assert result.ok
        assert result.output_valid
        assert result.output == {"heard": "hello", "via": "w9a"}
        assert adapter.invoked == ["echo-skill"]  # this class ran, not a builtin


class TestRefusals:
    def test_duplicate_registration_refuses_and_keeps_the_first(
        self, pristine_registry
    ) -> None:
        def first() -> SkillAdapter:
            return _ThirdPartyAdapter()

        register_adapter("taken-name", first)
        with pytest.raises(PaaError, match="already registered") as excinfo:
            register_adapter("taken-name", _ThirdPartyAdapter)

        assert excinfo.value.details["name"] == "taken-name"
        assert excinfo.value.details["registered_to"] == first.__qualname__
        assert adapter_for("taken-name") is first  # the first registration survives

    def test_a_builtin_name_cannot_be_shadowed_in_process(
        self, pristine_registry
    ) -> None:
        """The builtins register first — lazily, inside the same door — so a
        late registration reaching for their names meets the refusal, not a
        shadowed host adapter."""
        with pytest.raises(PaaError, match="already registered") as excinfo:
            register_adapter("native", _ThirdPartyAdapter)

        assert excinfo.value.details["name"] == "native"
        assert adapter_for("native") is NativeAdapter

    def test_an_empty_name_refuses(self, pristine_registry) -> None:
        with pytest.raises(PaaError, match="must not be empty"):
            register_adapter("", _ThirdPartyAdapter)

    def test_unknown_name_names_the_registered_adapters(
        self, pristine_registry
    ) -> None:
        with pytest.raises(PaaError, match="unknown skill adapter") as excinfo:
            adapter_for("never-registered")

        message = str(excinfo.value)
        for known in _BUILTIN_NAMES:
            assert known in message, f"{known!r} missing from the refusal: {message}"


class TestEntryPointDiscovery:
    def test_discovery_registers_resolvable_entries_in_sorted_order(
        self, pristine_registry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        late = _FakeEntryPoint("zeta-modality", lambda: _ThirdPartyAdapter)
        early = _FakeEntryPoint("alpha-modality", lambda: _ThirdPartyAdapter)
        _patch_entry_points(monkeypatch, [late, early])  # deliberately unsorted

        assert discover_entry_point_adapters() == ["alpha-modality", "zeta-modality"]
        assert adapter_for("alpha-modality") is _ThirdPartyAdapter

    def test_one_broken_plugin_is_skipped_and_the_rest_load(
        self, pristine_registry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _broken() -> Any:
            raise ImportError("acme_skills is not the module it claims to be")

        broken = _FakeEntryPoint("broken", _broken)
        working = _FakeEntryPoint("works", lambda: _ThirdPartyAdapter)
        _patch_entry_points(monkeypatch, [broken, working])

        with structlog.testing.capture_logs() as logs:
            registered = discover_entry_point_adapters()

        assert registered == ["works"]
        assert adapter_for("works") is _ThirdPartyAdapter
        skips = [e for e in logs if e["event"] == "skills.adapters.entry_point_skipped"]
        assert len(skips) == 1
        assert skips[0]["entry"] == "broken"
        assert "not the module it claims" in skips[0]["error"]

    def test_a_non_callable_entry_is_skipped_with_the_reason(
        self, pristine_registry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        not_a_factory = _FakeEntryPoint("module-only", lambda: importlib.metadata)
        working = _FakeEntryPoint("fine", lambda: _ThirdPartyAdapter)
        _patch_entry_points(monkeypatch, [not_a_factory, working])

        with structlog.testing.capture_logs() as logs:
            registered = discover_entry_point_adapters()

        assert registered == ["fine"]
        skips = [e for e in logs if e["event"] == "skills.adapters.entry_point_skipped"]
        assert skips[0]["entry"] == "module-only"
        assert "callable" in skips[0]["error"]

    def test_an_entry_reaching_for_a_builtin_name_is_skipped_not_fatal(
        self, pristine_registry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plugin that reaches for ``claw_hub`` is one broken plugin, not a
        host outage: the entry is skipped with a warning and the builtin it
        reached for is untouched."""
        shadow = _FakeEntryPoint("claw_hub", lambda: _ThirdPartyAdapter)
        own_name = _FakeEntryPoint("own-name", lambda: _ThirdPartyAdapter)
        _patch_entry_points(monkeypatch, [shadow, own_name])

        with structlog.testing.capture_logs() as logs:
            registered = discover_entry_point_adapters()

        assert registered == ["own-name"]
        assert adapter_for("claw_hub") is ClawHubAdapter
        skips = [e for e in logs if e["event"] == "skills.adapters.entry_point_skipped"]
        assert skips[0]["entry"] == "claw_hub"

    def test_a_miss_triggers_the_scan_once_per_process(
        self, pristine_registry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict[str, Any]] = []

        def _counting(**params: Any) -> list[_FakeEntryPoint]:
            calls.append(params)
            return []

        monkeypatch.setattr(importlib.metadata, "entry_points", _counting)

        with pytest.raises(PaaError, match="unknown skill adapter"):
            adapter_for("no-such-modality")
        with pytest.raises(PaaError, match="unknown skill adapter"):
            adapter_for("also-no-such-modality")

        assert calls == [{"group": "paa.skill_adapters"}]  # once, not twice

    def test_an_entry_point_plugin_is_reachable_through_the_lazy_scan(
        self, pristine_registry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The end-to-end third-party path: nobody calls discover explicitly —
        a miss against the builtins finds the installed distribution."""
        _patch_entry_points(
            monkeypatch,
            [_FakeEntryPoint("lazy-plugin", lambda: _ThirdPartyAdapter)],
        )
        assert adapter_for("lazy-plugin") is _ThirdPartyAdapter
