"""The pipeline registry v0 and explicit, mandate-scoped selection (C6.5).

What these tests pin:

* **the registry's two sources** — the seven in-process builtins
  (trust ``builtin``, runnable; Wave 7 added ``builtin.observe``, Wave 8
  added ``builtin.manager`` and ``improve.reflection-loop``, Wave 9
  added ``builtin.mcp-host``) and the local directory's manifests
  (trust ``local``, listed and inspected but never executed — running
  an out-of-process pipeline is P3's RPC transport);
* **the manifest contract at load** — an off-contract local manifest
  (unknown section, undeclarable capability, bad ``run.resume``, bad
  ``evidence.declares_tier``, off-contract ``[evidence]`` keys, missing
  identity keys, name that does not match its directory) is *named* in
  ``local_errors``, never silently dropped and never run;
* **selection is explicit and mandate-scoped, never scored** — explicit
  name validated against the registry then against the scope; a
  single-pipeline scope with no name selects it; a multi-pipeline
  scope with no name refuses; no name and no scope yields the default;
  a mandate whose scope does not list the requested pipeline refuses,
  naming the scope and the registered names;
* **the mandate carries the scope** — ``Scope.pipelines`` round-trips
  through the granted payload, and an empty list refuses at
  construction (it grants nothing and is almost certainly a typo).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from paa.config import PolicySettings
from paa.ledger.mandate import Scope, explicit_mandate
from paa.pipelines.registry import (
    CODING_FALLBACK_PIPELINE,
    DEFAULT_PIPELINE,
    LocalManifestError,
    PipelineRegistry,
    PipelineSelectionError,
    declaration_from_manifest,
    select,
)

_VALID_MANIFEST = """
[pipeline]
name = "tdd-loop"
version = "0.1.0"
protocol = "paa-pipeline/1"
description = "Write a failing test, make it pass, refactor."
requires_runtime = ">=4.1,<5"

[run]
command = ["python", "-m", "tdd_loop"]
resume = "journal"

[capabilities]
think = { roles = ["worker"] }
use_skill = ["read_file", "bash"]

[limits]
max_tokens = 200000
max_wall_s = 1800
max_calls = 400
max_sub_runs = 2

[params]
max_cycles = { type = "int", default = 5, min = 1, max = 20 }

[slots]
critic = { interface = "agent/1", default = "agent:critic" }
"""


def _local_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / "pipelines" / "tdd-loop").mkdir(parents=True)
    (home / "pipelines" / "tdd-loop" / "pipeline.toml").write_text(
        _VALID_MANIFEST, encoding="utf-8"
    )
    return home


class TestTheRegistry:
    def test_default_lists_the_seven_builtins(self) -> None:
        registry = PipelineRegistry(local_dir=None)
        rows = registry.list()
        assert [r.name for r in rows] == [
            "builtin.plan-execute-critique",
            "builtin.chat",
            "coder.tool-loop",
            "builtin.observe",
            "builtin.manager",
            "improve.reflection-loop",
            "builtin.mcp-host",
        ]
        assert all(r.trust == "builtin" and r.runnable for r in rows)

    def test_the_two_external_harness_adapters_declare_their_facts(self) -> None:
        """Pipelines #4 and #7 differ from the in-process five in the same
        two rows.

        ``resume = "none"`` (the spec §6 rule for external-harness
        adapters — the host refuses to mark such a run recoverable) and
        a declared honesty tier (``T1`` observed for the observer, ``T2``
        mediated for the mcp session) are the declarations' own
        statements, so a reader never has to infer recoverability from a
        pipeline's name.
        """
        registry = PipelineRegistry(local_dir=None)
        observe = registry.get("builtin.observe")
        mcp_host = registry.get("builtin.mcp-host")
        assert observe is not None and mcp_host is not None
        assert observe.declaration.resume == "none"
        assert observe.declaration.evidence_tier == "T1"
        assert observe.declaration.capabilities == {}
        assert mcp_host.declaration.resume == "none"
        assert mcp_host.declaration.evidence_tier == "T2"
        # The session's ask restates the six tools' own capability sets
        # (the chat turn's full set; sessions_list asks for nothing).
        assert mcp_host.declaration.capabilities == {
            "think": {"roles": ["assistant"]},
            "recall": True,
            "remember": True,
        }
        others = [
            r.declaration
            for r in registry.list()
            if r.name not in ("builtin.observe", "builtin.mcp-host")
        ]
        assert all(d.resume == "journal" for d in others)
        assert all(d.evidence_tier == "in-process" for d in others)

    def test_local_manifests_are_listed_not_run(self, tmp_path: Path) -> None:
        home = _local_home(tmp_path)
        registry = PipelineRegistry(local_dir=home / "pipelines")
        row = registry.get("tdd-loop")
        assert row is not None
        assert row.trust == "local"
        assert row.runnable is False
        assert row.declaration.params["max_cycles"].maximum == 20
        assert row.declaration.slots["critic"].interface == "agent/1"
        assert row.declaration.limits.max_tokens == 200000

    def test_off_contract_manifests_are_named_not_hidden(
        self, tmp_path: Path
    ) -> None:
        home = _local_home(tmp_path)
        bad = home / "pipelines" / "broken"
        bad.mkdir()
        (bad / "pipeline.toml").write_text(
            "[pipeline]\nname = 'broken'\nversion = '0.1'\n"
            "protocol = 'paa-pipeline/1'\nrequires_runtime = '>=4.1,<5'\n\n"
            "[limits]\nmax_tokens = 1\nmax_wall_s = 1\nmax_calls = 1\n"
            "max_sub_runs = 1\n\n[banana]\nx = 1\n",
            encoding="utf-8",
        )
        registry = PipelineRegistry(local_dir=home / "pipelines")
        assert "tdd-loop" in [r.name for r in registry.list()]
        assert "broken" in registry.local_errors
        assert "banana" in registry.local_errors["broken"]

    def test_name_directory_mismatch_is_named(self, tmp_path: Path) -> None:
        home = _local_home(tmp_path)
        other = home / "pipelines" / "other-name"
        other.mkdir()
        (other / "pipeline.toml").write_text(
            _VALID_MANIFEST, encoding="utf-8"  # name inside is tdd-loop
        )
        registry = PipelineRegistry(local_dir=home / "pipelines")
        assert "other-name" in registry.local_errors
        assert "does not match" in registry.local_errors["other-name"]

    def test_bad_resume_and_capability_refuse(self, tmp_path: Path) -> None:
        base = _local_home(tmp_path)
        manifest = base / "pipelines" / "tdd-loop" / "pipeline.toml"
        original = manifest.read_text(encoding="utf-8")
        # A bad resume value, in place.
        manifest.write_text(
            original.replace('resume = "journal"', "resume = 'sometimes'"),
            encoding="utf-8",
        )
        with pytest.raises(LocalManifestError, match="resume"):
            declaration_from_manifest(manifest)
        # An undeclarable capability key, inside the existing table.
        manifest.write_text(
            original.replace(
                'use_skill = ["read_file", "bash"]',
                'use_skill = ["read_file", "bash"]\nteleport = true',
            ),
            encoding="utf-8",
        )
        with pytest.raises(LocalManifestError, match="declarable"):
            declaration_from_manifest(manifest)
        manifest.write_text(original, encoding="utf-8")

    def test_evidence_tier_round_trips_and_off_contract_refuses(
        self, tmp_path: Path
    ) -> None:
        """The spec §6 facts a local adapter may declare (C7.1).

        A declared tier rides the declaration; a tier outside the ladder
        and an ``[evidence]`` key outside ``declares_tier`` are both
        refused at load, by name — the honesty ladder is closed
        vocabulary, not free text.
        """
        base = _local_home(tmp_path)
        manifest = base / "pipelines" / "tdd-loop" / "pipeline.toml"
        original = manifest.read_text(encoding="utf-8")

        manifest.write_text(
            original + '\n[evidence]\ndeclares_tier = "T1"\n', encoding="utf-8"
        )
        declaration = declaration_from_manifest(manifest)
        assert declaration.evidence_tier == "T1"
        assert declaration.resume == "journal"  # unchanged by the tier

        manifest.write_text(
            original + '\n[evidence]\ndeclares_tier = "T9"\n', encoding="utf-8"
        )
        with pytest.raises(LocalManifestError, match="declares_tier"):
            declaration_from_manifest(manifest)

        # A TOML list is unhashable; membership in the ladder must still be
        # a named refusal, not a TypeError escaping the parse contract.
        manifest.write_text(
            original + '\n[evidence]\ndeclares_tier = ["T1"]\n', encoding="utf-8"
        )
        with pytest.raises(LocalManifestError, match="declares_tier"):
            declaration_from_manifest(manifest)

        manifest.write_text(
            original + '\n[evidence]\ndeclares_tier = "T1"\nmood = "honest"\n',
            encoding="utf-8",
        )
        with pytest.raises(LocalManifestError, match="declarable set"):
            declaration_from_manifest(manifest)

        manifest.write_text(original, encoding="utf-8")

    @pytest.mark.parametrize(
        "old,new,reason",
        [
            ('protocol = "paa-pipeline/1"', 'protocol = "paa-pipeline/2"', "protocol"),
            (
                'requires_runtime = ">=4.1,<5"',
                'requires_runtime = "4.1"',
                "requires_runtime",
            ),
            ("max_calls = 400", "", "max_calls"),
            ('interface = "agent/1"', 'interface = "agent"', "<name>/<major>"),
            ('default = "agent:critic"', 'default = "memory:critic"', "default"),
            # TOML lists are unhashable: a membership test on one used to
            # raise TypeError and escape the named-error contract.
            ('resume = "journal"', 'resume = ["journal"]', "run.resume"),
            ('type = "int"', 'type = ["int"]', "expected one of"),
        ],
    )
    def test_each_contract_rule_is_named(
        self, tmp_path: Path, old: str, new: str, reason: str
    ) -> None:
        """Every ADR-0052 parse rule refuses, and names the manifest.

        A manifest that is off contract must land in ``local_errors``
        under its own name — silently dropping it would make ``list``
        lie about what exists in the local directory.
        """
        home = _local_home(tmp_path)
        manifest = home / "pipelines" / "tdd-loop" / "pipeline.toml"
        text = manifest.read_text(encoding="utf-8")
        assert old in text
        manifest.write_text(text.replace(old, new), encoding="utf-8")

        registry = PipelineRegistry(local_dir=home / "pipelines")
        assert "tdd-loop" not in [r.name for r in registry.list()]
        assert "tdd-loop" in registry.local_errors
        assert reason in registry.local_errors["tdd-loop"]

    def test_invalid_utf8_manifest_is_named(self, tmp_path: Path) -> None:
        """Corrupt bytes are a named refusal, not a decode traceback."""
        home = _local_home(tmp_path)
        manifest = home / "pipelines" / "tdd-loop" / "pipeline.toml"
        manifest.write_bytes(b"[pipeline]\nname = '\xff'\n")
        registry = PipelineRegistry(local_dir=home / "pipelines")
        assert "tdd-loop" not in [r.name for r in registry.list()]
        assert "UTF-8" in registry.local_errors["tdd-loop"]


class TestTheInstalledLayout:
    """The installer's staging root: ``installed/<name>/<version>/`` (C8.6).

    Staging IS registration: a package the five-gate installer staged is
    a registry row with trust ``installed`` that is **runnable** —
    through the sandboxed adapter, the loading contract the installer
    pinned — which is the one trust level whose rows execute without
    being in-process builtins. The declaration parses under the same
    closed contract as every local manifest, and the directory names
    must agree with it or the row is named in ``local_errors``, exactly
    as the flat layout's are.
    """

    def _stage(
        self, home: Path, name: str = "tdd-loop", version: str = "0.1.0"
    ) -> Path:
        staged = home / "pipelines" / "installed" / name / version
        staged.mkdir(parents=True)
        (staged / "pipeline.toml").write_text(
            _VALID_MANIFEST, encoding="utf-8"
        )  # the manifest's own name is tdd-loop
        return staged

    def test_a_staged_package_registers_runnable(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        self._stage(home)
        registry = PipelineRegistry(local_dir=home / "pipelines")
        row = registry.get("tdd-loop")
        assert row is not None
        assert row.trust == "installed"
        assert row.runnable is True
        assert row.version == "0.1.0"
        assert row.source.endswith("installed/tdd-loop/0.1.0/pipeline.toml")
        assert registry.local_errors == {}

    def test_a_name_mismatch_is_named_by_directory(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        self._stage(home, name="other-name")
        registry = PipelineRegistry(local_dir=home / "pipelines")
        assert registry.get("tdd-loop") is None
        assert "other-name/0.1.0" in registry.local_errors
        assert "does not match" in registry.local_errors["other-name/0.1.0"]

    def test_a_version_mismatch_is_named_by_directory(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        self._stage(home, version="9.9.9")
        registry = PipelineRegistry(local_dir=home / "pipelines")
        assert registry.get("tdd-loop") is None
        assert "tdd-loop/9.9.9" in registry.local_errors
        assert "version" in registry.local_errors["tdd-loop/9.9.9"]

    def test_an_off_contract_staged_manifest_is_named(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        staged = self._stage(home)
        (staged / "pipeline.toml").write_text(
            (staged / "pipeline.toml").read_text(encoding="utf-8") + "\n[banana]\nx = 1\n",
            encoding="utf-8",
        )
        registry = PipelineRegistry(local_dir=home / "pipelines")
        assert registry.get("tdd-loop") is None
        assert "banana" in registry.local_errors["tdd-loop/0.1.0"]

    def test_the_installed_name_belongs_to_the_staging_root(
        self, tmp_path: Path
    ) -> None:
        """A directory named ``installed`` is the staging root, not a
        local pipeline: the flat scan reads ``<local>/<name>/pipeline.toml``
        and the root carries none, so nothing phantom registers and
        nothing errors."""
        home = tmp_path / "home"
        (home / "pipelines" / "installed" / "demo.echo" / "1.0.0").mkdir(parents=True)
        registry = PipelineRegistry(local_dir=home / "pipelines")
        assert [r.name for r in registry.list()] == [
            r.name for r in PipelineRegistry(local_dir=None).list()
        ]
        assert registry.local_errors == {}

    def test_refresh_sees_a_newly_staged_package(self, tmp_path: Path) -> None:
        """A registry constructed before the install sees it after a
        refresh — and when a flat local manifest and a staged package
        share a name, both list, and ``get`` answers with the operator's
        hand-placed one: the local directory is the operator's word, and
        the staged row never silently shadows it."""
        home = _local_home(tmp_path)
        registry = PipelineRegistry(local_dir=home / "pipelines")
        assert registry.get("tdd-loop") is not None  # the flat local manifest

        self._stage(home)
        registry.refresh()
        by_trust = {r.trust for r in registry.list() if r.name == "tdd-loop"}
        assert by_trust == {"local", "installed"}  # both rows, nothing hidden
        row = registry.get("tdd-loop")
        assert row is not None
        assert row.trust == "local"  # the operator's directory is their word

        # Without the flat manifest, the staged row is the one `get` answers
        # with — the shape an install into a clean home leaves behind.
        clean = tmp_path / "clean-home"
        self._stage(clean)
        clean_registry = PipelineRegistry(local_dir=clean / "pipelines")
        clean_row = clean_registry.get("tdd-loop")
        assert clean_row is not None
        assert clean_row.trust == "installed"
        assert clean_row.runnable is True


class TestSelection:
    def _registry(self, tmp_path: Path) -> PipelineRegistry:
        return PipelineRegistry(local_dir=_local_home(tmp_path) / "pipelines")

    def test_explicit_name_selects(self, tmp_path: Path) -> None:
        row = select("builtin.chat", registry=self._registry(tmp_path))
        assert row.name == "builtin.chat"

    def test_unknown_name_names_the_registered(self, tmp_path: Path) -> None:
        with pytest.raises(PipelineSelectionError, match=r"coder\.tool-loop"):
            select("nope", registry=self._registry(tmp_path))

    def test_mandate_scope_refuses_unlisted_pipeline(self, tmp_path: Path) -> None:
        with pytest.raises(PipelineSelectionError, match="outside this mandate"):
            select(
                "coder.tool-loop",
                registry=self._registry(tmp_path),
                allowed=("builtin.chat",),
            )

    def test_mandate_scope_allows_its_own(self, tmp_path: Path) -> None:
        row = select(
            "builtin.chat",
            registry=self._registry(tmp_path),
            allowed=("builtin.chat", "coder.tool-loop"),
        )
        assert row.name == "builtin.chat"

    def test_single_pipeline_scope_selects_itself(self, tmp_path: Path) -> None:
        row = select(None, registry=self._registry(tmp_path), allowed=("builtin.chat",))
        assert row.name == "builtin.chat"

    def test_multi_pipeline_scope_without_a_name_refuses(self, tmp_path: Path) -> None:
        with pytest.raises(PipelineSelectionError, match="name one"):
            select(
                None,
                registry=self._registry(tmp_path),
                allowed=("builtin.chat", "coder.tool-loop"),
            )

    def test_no_name_no_scope_is_the_default(self, tmp_path: Path) -> None:
        row = select(None, registry=self._registry(tmp_path))
        assert row.name == DEFAULT_PIPELINE

    def test_empty_scope_grants_nothing(self, tmp_path: Path) -> None:
        with pytest.raises(PipelineSelectionError, match="empty"):
            select("builtin.chat", registry=self._registry(tmp_path), allowed=[])


class TestTheCodingFallback:
    """C8.3 — the coder's re-scope, pinned where selection vocabulary lives.

    The coder loop is *the preliminary coding loop*: the in-process
    fallback a run reaches for when it needs code changed and no external
    harness is attached. External harnesses arrive via ``paa record`` /
    ``paa thread`` instead. These pins hold the position to the registry
    (the one place that answers "which pipelines exist"), so a rename or
    a silent de-scope of the fallback is a red test, not a docs drift.
    """

    def test_the_fallback_is_the_coder_tool_loop(self, tmp_path: Path) -> None:
        registry = PipelineRegistry.default(home=tmp_path)
        row = registry.get(CODING_FALLBACK_PIPELINE)
        assert row is not None, "the coding fallback must stay registered"
        assert row.name == "coder.tool-loop"

    def test_the_fallback_selects_explicitly(self, tmp_path: Path) -> None:
        row = select(
            CODING_FALLBACK_PIPELINE, registry=PipelineRegistry.default(home=tmp_path)
        )
        assert row.name == CODING_FALLBACK_PIPELINE

    def test_the_fallback_is_not_the_task_default(self) -> None:
        # Two different defaults with two different meanings: the task
        # default runs when *nothing* is named; the coding fallback is
        # what a coding-flavoured surface reaches for. Collapsing them
        # would make every unnamed run a coding run.
        assert CODING_FALLBACK_PIPELINE != DEFAULT_PIPELINE

    def test_the_declaration_carries_the_position(self, tmp_path: Path) -> None:
        registry = PipelineRegistry.default(home=tmp_path)
        row = registry.get(CODING_FALLBACK_PIPELINE)
        assert row is not None
        description = row.declaration.description.lower()
        assert "fallback" in description
        assert "external harness" in description
        assert "paa record" in description and "paa thread" in description


class TestTheGuardedSelfImprovementLoop:
    """C8.1 — the guarded self-improvement loop, pinned where selection
    vocabulary lives (the pattern ``TestTheCodingFallback`` set for C8.3).

    The loop must stay *guarded*: every improvement lands as an attested
    change (C7.2), no gate is ever model-decided, and the refusal paths
    are first-class. These pins hold that position to the registry — the
    one place that answers "which pipelines exist" — so a silent
    de-scoping, a capability widening or a rename is a red test, not a
    docs drift.
    """

    def test_the_loop_is_registered_and_runnable(self, tmp_path: Path) -> None:
        registry = PipelineRegistry.default(home=tmp_path)
        row = registry.get("improve.reflection-loop")
        assert row is not None, "the guarded self-improvement loop must stay registered"
        assert row.runnable
        assert row.trust == "builtin"

    def test_the_loop_selects_explicitly(self, tmp_path: Path) -> None:
        row = select(
            "improve.reflection-loop",
            registry=PipelineRegistry.default(home=tmp_path),
        )
        assert row.name == "improve.reflection-loop"

    def test_the_capability_ask_is_commit_and_nothing_else(self, tmp_path: Path) -> None:
        """The machine-readable half of "no gate is ever model-decided".

        The loop asks for ``commit`` alone: no ``think``, no
        ``use_skill`` — a declaration that grew a model-shaped capability
        would be a loop whose gate could be model-decided, and this pin
        makes that widening a review conversation instead of a drift.
        """
        registry = PipelineRegistry.default(home=tmp_path)
        row = registry.get("improve.reflection-loop")
        assert row is not None
        assert set(row.declaration.capabilities) == {"commit"}
        assert row.declaration.capabilities["commit"] is True

    def test_the_declaration_carries_the_guards(self, tmp_path: Path) -> None:
        registry = PipelineRegistry.default(home=tmp_path)
        row = registry.get("improve.reflection-loop")
        assert row is not None
        description = row.declaration.description.lower()
        assert "reflection" in description
        assert "human gate" in description
        assert "attested change bundle" in description
        # The honesty line, on the selection surface: the declaration
        # says what the loop is not (no model, no meta-learning).
        assert row.declaration.resume == "journal"
        assert row.declaration.evidence_tier == "in-process"


class TestTheMcpHostAdapter:
    """W9-C9.4 — P6's last adapter, pinned where selection vocabulary lives
    (the pattern ``TestTheCodingFallback`` set for C8.3 and
    ``TestTheGuardedSelfImprovementLoop`` for C8.1).

    The externally-driven MCP session must stay *a registration over the
    unchanged server*: the six tools' policy gate and journaling live in
    ``src/paa/mcp/server.py`` and are not re-expressed anywhere else; the
    declaration is the machine-readable ask, the tier and the
    resume stance. A silent de-scoping, a capability widening or a rename
    is a red test here, not a docs drift.
    """

    def test_the_session_pipeline_is_registered_and_runnable(self) -> None:
        registry = PipelineRegistry(local_dir=None)
        row = registry.get("builtin.mcp-host")
        assert row is not None, "the mcp-host adapter must stay registered"
        assert row.runnable
        assert row.trust == "builtin"

    def test_the_session_selects_explicitly(self) -> None:
        row = select("builtin.mcp-host", registry=PipelineRegistry(local_dir=None))
        assert row.name == "builtin.mcp-host"

    def test_the_ask_restates_the_six_tools_policy_classes(self) -> None:
        """The declaration's ask is the TOOL_MANIFEST union, restated.

        The union is derived from the manifest in
        :mod:`paa.mcp.host` (``MCP_HOST_CAPABILITIES``); the declaration
        restates it in the manifest's own vocabulary so an overlay may
        narrow it. A tool whose manifest row grows a capability widens
        the union — this pin turns that widening into a red test here
        until the declaration follows, the same drift lock
        ``test_declarations_restate_the_classes`` applies to identity.
        """
        from paa.mcp.host import MCP_HOST_CAPABILITIES
        from paa.pipelines.base import Capability

        registry = PipelineRegistry(local_dir=None)
        row = registry.get("builtin.mcp-host")
        assert row is not None
        declared = row.declaration.capabilities
        assert set(declared) == {"think", "recall", "remember"}
        expected = {
            "think": {"roles": ["assistant"]},
            "recall": True,
            "remember": True,
        }
        assert declared == expected
        # And the class-level ask is the manifest union itself.
        assert frozenset(
            {Capability.THINK, Capability.RECALL, Capability.REMEMBER}
        ) == MCP_HOST_CAPABILITIES

    def test_the_declaration_carries_the_position(self) -> None:
        registry = PipelineRegistry(local_dir=None)
        row = registry.get("builtin.mcp-host")
        assert row is not None
        description = row.declaration.description.lower()
        assert "externally-driven mcp session" in description
        assert "six" in description and "tools" in description
        assert "t2" in description
        assert "not replayable" in description
        # Honest limits: the session is externally paced, so the facade
        # declares nothing it cannot enforce.
        limits = row.declaration.limits
        assert limits.max_tokens is None
        assert limits.max_wall_s is None
        assert limits.max_calls is None
        assert limits.max_sub_runs is None
        # No params, no slots: the session has no knobs this door honours.
        assert row.declaration.params == {}
        assert row.declaration.slots == {}


class TestTheMandateScope:
    def _policy(self) -> PolicySettings:
        return PolicySettings()

    def test_pipelines_round_trip_through_the_payload(self) -> None:
        mandate = explicit_mandate(
            kind="cli",
            requester_id="operator",
            policy=self._policy(),
            pipelines=["builtin.chat", "coder.tool-loop"],
        )
        payload = mandate.granted_payload(session_id="s1")
        assert payload["scope"]["pipelines"] == [
            "builtin.chat",
            "coder.tool-loop",
        ]
        assert mandate.scope.pipelines == ("builtin.chat", "coder.tool-loop")

    def test_no_pipelines_stays_unconstrained(self) -> None:
        mandate = explicit_mandate(
            kind="cli", requester_id="operator", policy=self._policy()
        )
        assert mandate.scope.pipelines is None
        # The golden v1 scope shape is unchanged for an unconstrained
        # mandate: the pipelines key is absent, not null — payloads
        # granted before C6.5 read back byte-for-byte.
        scope = mandate.granted_payload(session_id="s1")["scope"]
        assert "pipelines" not in scope

    def test_an_empty_scope_list_refuses_construction(self) -> None:
        with pytest.raises(ValueError, match="at least one pipeline"):
            Scope(pipelines=())

    def test_scope_fields_default_untouched(self) -> None:
        payload = explicit_mandate(
            kind="cli", requester_id="operator", policy=self._policy()
        ).granted_payload(session_id="s1")
        scope: dict[str, Any] = payload["scope"]
        # The golden v1 keys, and only them, for an unconstrained scope.
        assert set(scope) == {"workspace", "permission_mode", "budget"}
