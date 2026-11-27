"""The publish side of the pipeline marketplace (W9 C9.1, the spec's P5 full).

What these tests prove is the round trip the row names: a publish writes
package bytes plus an index entry the EXISTING ``LocalDirectoryRegistry``
lists and fetches; the publisher's key is generated on first use and
never travels; an unsigned publish refuses with a named reason unless the
operator opts in (the install escape hatch's flag and meaning, mirrored);
and the compatibility floor is checked against the running version on
both ends of the economy.

Publishing is a host-side operator action (the C7.2 host-git precedent),
so none of these tests assert ledger rows — there are none to assert.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from paa import __version__
from paa.marketplace.package import PIPELINE_KIND
from paa.marketplace.registry_client import LocalDirectoryRegistry
from paa.marketplace.signing import generate_keypair, signing_is_secure, verify
from paa.pipelines.publish import (
    KEYS_DIR_NAME,
    PUBLISHER_KEY_NAME,
    PUBLISHER_NAME,
    publish_pipeline_package,
)

_DECLARATION = """\
[pipeline]
name = "demo.echo"
version = "1.2.3"
protocol = "paa-pipeline/1"
description = "A fixture pipeline that echoes its goal."
requires_runtime = ">=4.1,<5"

[run]
resume = "journal"

[evidence]
declares_tier = "unrated"

[capabilities]
think = { roles = ["worker"] }

[limits]
max_tokens = 100000
max_wall_s = 600
max_calls = 100
max_sub_runs = 1
"""

_ENTRYPOINT = """\
import json, sys

args_path = sys.argv[sys.argv.index("--paa-args") + 1]
with open(args_path, encoding="utf-8") as fh:
    args = json.load(fh)
goal = args.get("goal") or ""
print(json.dumps({
    "status": "completed",
    "summary": "echo: " + goal,
    "metrics": {},
}))
"""


def _package_dir(
    root: Path,
    *,
    declaration: str = _DECLARATION,
    entrypoint: str = _ENTRYPOINT,
) -> Path:
    """A pipeline-package source directory, off the tree's own scratch."""
    source = root / f"src-{uuid.uuid4().hex[:6]}"
    source.mkdir(parents=True)
    (source / "pipeline.toml").write_text(declaration, encoding="utf-8")
    (source / "pipeline.py").write_text(entrypoint, encoding="utf-8")
    return source


def _staged_install(
    home: Path, name: str, version: str, *, review: bool = True
) -> Path:
    """An installed pipeline's staged layout, written by hand.

    Publish reads the layout the install flow writes; staging it by hand
    keeps these tests on publish's own contract (the bytes, the
    ``pipeline.toml``, the review artifact that must NOT travel) without
    paying six real stages per fixture.
    """
    staged = home / "pipelines" / "installed" / name / version
    staged.mkdir(parents=True)
    (staged / "pipeline.toml").write_text(
        _DECLARATION.replace('name = "demo.echo"', f'name = "{name}"').replace(
            'version = "1.2.3"', f'version = "{version}"'
        ),
        encoding="utf-8",
    )
    (staged / "pipeline.py").write_text(_ENTRYPOINT, encoding="utf-8")
    if review:
        (staged / "capability-review.json").write_text(
            json.dumps({"decision": "approved"}), encoding="utf-8"
        )
    return staged


class TestPublishHappyPath:
    """A signed publish round-trips through the registry that reads it."""

    async def test_a_signed_publish_round_trips_through_the_registry(
        self, tmp_path: Path
    ) -> None:
        if not signing_is_secure():
            pytest.skip(
                "real signing backend not available; dev fallback cannot authenticate"
            )

        source = _package_dir(tmp_path)
        hub = tmp_path / "hub"
        home = tmp_path / "home"

        report = await publish_pipeline_package(
            str(source), hub=hub, home=home
        )

        assert report.published
        assert report.signed
        assert report.package_name == "demo.echo"
        assert report.version == "1.2.3"
        assert report.hub == hub
        assert report.package_path == hub / "demo.echo-1.2.3.paapkg"
        assert report.package_path.is_file()

        # The round-trip property: what publish writes, the EXISTING
        # registry lists and fetches.
        registry = LocalDirectoryRegistry(hub)
        hits = await registry.search("demo.echo")
        assert [h.package_name for h in hits] == ["demo.echo"]
        entry = hits[0]
        assert entry.kind == PIPELINE_KIND
        assert entry.publisher == PUBLISHER_NAME
        assert entry.publisher_key == report.publisher_key
        assert entry.signature
        assert entry.requires_paa == ">=4.1,<5"
        assert entry.protocol == "paa-pipeline/1"

        fetched = await registry.fetch("demo.echo", "1.2.3")
        assert fetched.verify_content_hash()
        assert fetched.manifest.publisher_key == entry.publisher_key
        assert fetched.manifest.signature == entry.signature
        assert fetched.manifest.requires_paa == ">=4.1,<5"
        assert fetched.manifest.protocol == "paa-pipeline/1"
        # The signature verifies under the publisher's own public key —
        # and the compat fields are inside the signed bytes.
        assert verify(
            fetched.manifest.signing_bytes(),
            fetched.manifest.signature,
            fetched.manifest.publisher_key,
        )

    async def test_the_private_key_never_leaves_home(self, tmp_path: Path) -> None:
        if not signing_is_secure():
            pytest.skip(
                "real signing backend not available; dev fallback cannot authenticate"
            )

        source = _package_dir(tmp_path)
        hub = tmp_path / "hub"
        home = tmp_path / "home"

        report = await publish_pipeline_package(
            str(source), hub=hub, home=home
        )

        key_file = home / KEYS_DIR_NAME / PUBLISHER_KEY_NAME
        assert key_file.is_file()
        private_b64 = json.loads(key_file.read_text(encoding="utf-8"))["private_b64"]
        # Not in the package, not in the index, not in the report.
        package_bytes = report.package_path.read_bytes() if report.package_path else b""
        assert private_b64.encode() not in package_bytes
        index_text = (hub / "index.json").read_text(encoding="utf-8")
        assert private_b64 not in index_text
        assert private_b64 not in report.publisher_key

    async def test_the_publisher_key_is_generated_once_and_reused(
        self, tmp_path: Path
    ) -> None:
        if not signing_is_secure():
            pytest.skip(
                "real signing backend not available; dev fallback cannot authenticate"
            )

        source = _package_dir(tmp_path)
        hub = tmp_path / "hub"
        home = tmp_path / "home"

        first = await publish_pipeline_package(str(source), hub=hub, home=home)
        assert first.published and first.key_generated

        # The key file is owner-only: the private half lives under
        # PAA_HOME and the mode is the filesystem's half of that promise.
        key_file = home / KEYS_DIR_NAME / PUBLISHER_KEY_NAME
        assert (key_file.stat().st_mode & 0o777) == 0o600

        second = await publish_pipeline_package(
            str(_package_dir(tmp_path)), hub=hub, home=home
        )
        assert second.published and not second.key_generated
        assert second.publisher_key == first.publisher_key

    async def test_an_explicit_key_overrides_the_home_key(
        self, tmp_path: Path
    ) -> None:
        if not signing_is_secure():
            pytest.skip(
                "real signing backend not available; dev fallback cannot authenticate"
            )

        pair = generate_keypair()
        key_file = tmp_path / "operator-key.json"
        key_file.write_text(
            json.dumps(
                {"v": 1, "private_b64": pair.private_b64, "public_b64": pair.public_b64}
            ),
            encoding="utf-8",
        )
        source = _package_dir(tmp_path)
        home = tmp_path / "home"

        report = await publish_pipeline_package(
            str(source), hub=tmp_path / "hub", home=home, key=key_file
        )

        assert report.published
        assert report.publisher_key == pair.public_b64
        # The home default was never minted: --key is an override, not a
        # second identity.
        assert not (home / KEYS_DIR_NAME / PUBLISHER_KEY_NAME).exists()


class TestPublishRefusals:
    """Every refusal names its reason and leaves the hub exactly as it was."""

    async def test_an_unreadable_key_refuses_with_a_named_reason(
        self, tmp_path: Path
    ) -> None:
        source = _package_dir(tmp_path)
        bad_key = tmp_path / "bad-key.json"
        bad_key.write_text("{not json", encoding="utf-8")
        hub = tmp_path / "hub"

        report = await publish_pipeline_package(
            str(source), hub=hub, home=tmp_path / "home", key=bad_key
        )

        assert not report.published
        assert "could not be read" in (report.reason or "")
        # Zero trace: nothing was written to the hub.
        assert not (hub / "index.json").exists()
        assert not list(hub.glob("*.paapkg"))

    async def test_the_insecure_fallback_refuses_to_sign(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = _package_dir(tmp_path)
        hub = tmp_path / "hub"

        import paa.pipelines.publish as publish_module

        monkeypatch.setattr(publish_module, "signing_is_secure", lambda: False)
        report = await publish_pipeline_package(
            str(source), hub=hub, home=tmp_path / "home"
        )

        assert not report.published
        assert "insecure HMAC dev fallback" in (report.reason or "")
        assert not (hub / "index.json").exists()

    async def test_allow_unverified_publishes_unsigned(
        self, tmp_path: Path
    ) -> None:
        source = _package_dir(tmp_path)
        hub = tmp_path / "hub"

        report = await publish_pipeline_package(
            str(source), hub=hub, home=tmp_path / "home", allow_unverified=True
        )

        assert report.published
        assert not report.signed
        registry = LocalDirectoryRegistry(hub)
        fetched = await registry.fetch("demo.echo", "1.2.3")
        assert fetched.manifest.signature == ""
        assert fetched.manifest.publisher_key == ""
        # The compatibility fields still travel: they are metadata, not
        # a trust claim.
        assert fetched.manifest.requires_paa == ">=4.1,<5"

    async def test_a_requires_paa_that_excludes_the_running_version_refuses(
        self, tmp_path: Path
    ) -> None:
        source = _package_dir(
            tmp_path,
            declaration=_DECLARATION.replace(
                'requires_runtime = ">=4.1,<5"', 'requires_runtime = ">=99,<100"'
            ),
        )
        hub = tmp_path / "hub"

        report = await publish_pipeline_package(
            str(source), hub=hub, home=tmp_path / "home"
        )

        assert not report.published
        assert "'>=99,<100'" in (report.reason or "")
        assert "excludes the running version" in (report.reason or "")
        assert __version__ in (report.reason or "")
        assert not (hub / "index.json").exists()

    async def test_a_directory_without_a_declaration_refuses(
        self, tmp_path: Path
    ) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        hub = tmp_path / "hub"

        report = await publish_pipeline_package(
            str(empty), hub=hub, home=tmp_path / "home"
        )

        assert not report.published
        assert "no pipeline.toml" in (report.reason or "")

    async def test_a_remote_hub_url_refuses_with_a_named_reason(
        self, tmp_path: Path
    ) -> None:
        source = _package_dir(tmp_path)

        report = await publish_pipeline_package(
            str(source),
            hub="https://hub.example.test",
            home=tmp_path / "home",
            allow_unverified=True,
        )

        assert not report.published
        assert "remote hub is not supported yet" in (report.reason or "")


class TestPublishFromInstalled:
    """The re-stage path: an installed pipeline's bytes become a package."""

    async def test_an_installed_pipeline_restages_minus_its_review_artifact(
        self, tmp_path: Path
    ) -> None:
        if not signing_is_secure():
            pytest.skip(
                "real signing backend not available; dev fallback cannot authenticate"
            )

        home = tmp_path / "home"
        _staged_install(home, "demo.echo", "1.2.3")
        hub = tmp_path / "hub"

        report = await publish_pipeline_package(
            "demo.echo", hub=hub, home=home, version="1.2.3"
        )

        assert report.published
        fetched = await LocalDirectoryRegistry(hub).fetch("demo.echo", "1.2.3")
        assert set(fetched.files) == {"pipeline.toml", "pipeline.py"}
        assert "capability-review.json" not in fetched.files
        assert fetched.verify_content_hash()

    async def test_the_version_argument_selects_among_staged_versions(
        self, tmp_path: Path
    ) -> None:
        if not signing_is_secure():
            pytest.skip(
                "real signing backend not available; dev fallback cannot authenticate"
            )

        home = tmp_path / "home"
        _staged_install(home, "demo.echo", "1.1.0")
        _staged_install(home, "demo.echo", "1.2.3")
        _staged_install(home, "demo.echo", "1.0.0")
        hub = tmp_path / "hub"

        report = await publish_pipeline_package(
            "demo.echo", hub=hub, home=home, version="1.1.0"
        )

        assert report.published
        assert report.version == "1.1.0"

    async def test_version_omitted_publishes_the_newest_staged(
        self, tmp_path: Path
    ) -> None:
        if not signing_is_secure():
            pytest.skip(
                "real signing backend not available; dev fallback cannot authenticate"
            )

        home = tmp_path / "home"
        _staged_install(home, "demo.echo", "1.1.0")
        _staged_install(home, "demo.echo", "1.2.3")
        _staged_install(home, "demo.echo", "1.0.0")

        report = await publish_pipeline_package(
            "demo.echo", hub=tmp_path / "hub", home=home
        )

        assert report.published
        assert report.version == "1.2.3"

    async def test_an_unknown_installed_name_refuses_naming_what_is_there(
        self, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        _staged_install(home, "demo.echo", "1.2.3")

        report = await publish_pipeline_package(
            "demo.none", hub=tmp_path / "hub", home=home
        )

        assert not report.published
        assert "demo.none" in (report.reason or "")
        assert "demo.echo" in (report.reason or "")

    async def test_a_missing_staged_version_refuses(
        self, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        _staged_install(home, "demo.echo", "1.2.3")

        report = await publish_pipeline_package(
            "demo.echo", hub=tmp_path / "hub", home=home, version="9.9.9"
        )

        assert not report.published
        assert "no staged version" in (report.reason or "")
        assert "1.2.3" in (report.reason or "")

    async def test_version_with_a_directory_source_refuses(
        self, tmp_path: Path
    ) -> None:
        source = _package_dir(tmp_path)

        report = await publish_pipeline_package(
            str(source), hub=tmp_path / "hub", home=tmp_path / "home", version="1.2.3"
        )

        assert not report.published
        assert "--version selects among an installed" in (report.reason or "")
