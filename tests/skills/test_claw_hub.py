"""ClawHubAdapter: parse SKILL.md directories and run their entrypoints."""

from __future__ import annotations

from pathlib import Path

import pytest

from paa.core.errors import SkillContractError
from paa.core.types import Permission
from paa.sandbox.dryrun_backend import DryRunSandbox
from paa.sandbox.subprocess_backend import SubprocessSandbox
from paa.skills.adapters.claw_hub import ClawHubAdapter, parse_skill_md

# A tiny entrypoint: reads its arguments from the --paa-args file, echoes a
# greeting as a single JSON object on stdout.
_ENTRYPOINT = """\
import json, sys
args_path = sys.argv[sys.argv.index("--paa-args") + 1]
with open(args_path, encoding="utf-8") as fh:
    args = json.load(fh)
print(json.dumps({"greeting": "hello " + args.get("name", "world")}))
"""

_SKILL_MD = """\
---
name: greeter
description: Greets whoever is named in the arguments, in one short line.
version: 1.0.0
permissions: [PERM_SANDBOX_RUN]
input_schema: {"type": "object", "properties": {"name": {"type": "string"}}}
output_schema:
  type: object
  properties:
    greeting:
      type: string
  required: [greeting]
---
You are a greeter. Say hello to the named person.
"""


def _make_skill(root: Path, *, skill_md: str = _SKILL_MD, entrypoint: str = _ENTRYPOINT) -> Path:
    skill_dir = root / "greeter"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    (skill_dir / "main.py").write_text(entrypoint, encoding="utf-8")
    return skill_dir


class TestParseSkillMd:
    def test_no_frontmatter_is_all_body(self) -> None:
        fm, body = parse_skill_md("Just a body, no fences here.")
        assert fm == {}
        assert body == "Just a body, no fences here."

    def test_frontmatter_and_body_split(self) -> None:
        fm, body = parse_skill_md(_SKILL_MD)
        assert fm["name"] == "greeter"
        assert fm["permissions"] == ["PERM_SANDBOX_RUN"]
        assert body.startswith("You are a greeter")

    def test_unterminated_fence_raises(self) -> None:
        with pytest.raises(SkillContractError, match="never closed"):
            parse_skill_md("---\nname: broken\nstill inside frontmatter")


class TestDiscover:
    async def test_discovers_skill_in_subdirectory(self, tmp_path: Path) -> None:
        _make_skill(tmp_path)
        contracts = await ClawHubAdapter(tmp_path).discover()
        assert len(contracts) == 1
        contract = contracts[0]
        assert contract.skill_name == "greeter"
        assert contract.provider == "claw_hub"
        assert Permission.SANDBOX_RUN in contract.required_permissions
        assert contract.invocation.system_prompt is not None
        assert "greeter" in contract.invocation.system_prompt.lower()

    async def test_discovers_single_skill_directory_as_root(self, tmp_path: Path) -> None:
        skill_dir = _make_skill(tmp_path)
        contracts = await ClawHubAdapter(skill_dir).discover()
        assert [c.skill_name for c in contracts] == ["greeter"]

    async def test_malformed_skill_md_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        _make_skill(tmp_path)  # a good one
        bad = tmp_path / "broken"
        bad.mkdir()
        (bad / "SKILL.md").write_text("---\nname: broken\nno closing fence", encoding="utf-8")
        (bad / "main.py").write_text("print('{}')", encoding="utf-8")
        contracts = await ClawHubAdapter(tmp_path).discover()
        # The good skill still surfaces; the broken one is dropped.
        assert [c.skill_name for c in contracts] == ["greeter"]

    async def test_missing_description_raises_on_build(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "terse"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: terse\n---\nhi", encoding="utf-8")
        (skill_dir / "main.py").write_text("print('{}')", encoding="utf-8")
        with pytest.raises(SkillContractError, match="description"):
            ClawHubAdapter(skill_dir).build_contract(skill_dir)


class TestInvoke:
    async def test_runs_entrypoint_and_captures_json(self, tmp_path: Path) -> None:
        _make_skill(tmp_path)
        adapter = ClawHubAdapter(tmp_path)
        contract = (await adapter.discover())[0]

        result = await adapter.invoke(
            contract,
            {"name": "vishal"},
            sandbox=SubprocessSandbox(),
            timeout=30.0,
            secret_broker=None,
        )
        assert result.ok, result.error
        assert result.output == {"greeting": "hello vishal"}
        # The captured output is schema-valid for the declared output_schema.
        assert contract.validate_output(result.output) == []

    async def test_nonzero_exit_is_a_skill_failure_not_a_crash(self, tmp_path: Path) -> None:
        _make_skill(tmp_path, entrypoint="import sys; sys.exit(3)")
        adapter = ClawHubAdapter(tmp_path)
        contract = (await adapter.discover())[0]
        result = await adapter.invoke(
            contract, {}, sandbox=SubprocessSandbox(), timeout=30.0, secret_broker=None
        )
        assert result.ok is False
        assert result.exit_code == 3

    async def test_non_json_stdout_is_a_skill_failure(self, tmp_path: Path) -> None:
        _make_skill(tmp_path, entrypoint="print('this is not json')")
        adapter = ClawHubAdapter(tmp_path)
        contract = (await adapter.discover())[0]
        result = await adapter.invoke(
            contract, {}, sandbox=SubprocessSandbox(), timeout=30.0, secret_broker=None
        )
        assert result.ok is False
        assert "JSON" in (result.error or "")

    async def test_requires_a_sandbox(self, tmp_path: Path) -> None:
        _make_skill(tmp_path)
        adapter = ClawHubAdapter(tmp_path)
        contract = (await adapter.discover())[0]
        with pytest.raises(SkillContractError, match="require a sandbox"):
            await adapter.invoke(contract, {}, sandbox=None, timeout=1.0, secret_broker=None)

    async def test_dryrun_sandbox_canned_output(self, tmp_path: Path) -> None:
        _make_skill(tmp_path)
        adapter = ClawHubAdapter(tmp_path)
        contract = (await adapter.discover())[0]
        # DryRun returns whatever stdout it was constructed with — proving the
        # adapter reads the sandbox result rather than executing anything itself.
        sandbox = DryRunSandbox(stdout='{"greeting": "canned"}')
        result = await adapter.invoke(
            contract, {"name": "x"}, sandbox=sandbox, timeout=1.0, secret_broker=None
        )
        assert result.ok
        assert result.output == {"greeting": "canned"}
        assert sandbox.last_invocation is not None
        # Arguments must never ride in the child environment.
        assert "name" not in sandbox.last_invocation["env"]
        assert not any(v == "x" for v in sandbox.last_invocation["env"].values())
