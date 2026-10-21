"""Marketplace: signing, packaging, the local registry, and the five gates.

The installer tests are the important ones — each proves a specific gate rejects
a specific attack, and that a rejected package leaves ZERO trace in the registry.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import structlog.testing

from paa.core.types import PermissionMode
from paa.marketplace.installer import InstallGate, PackageInstaller
from paa.marketplace.package import PackageError, PackageManifest, SkillPackage
from paa.marketplace.registry_client import LocalDirectoryRegistry
from paa.marketplace.signing import generate_keypair, sign, signing_is_secure, verify
from paa.storage.relational.database import Database


def _manifest(**over: object) -> PackageManifest:
    base = {
        "package_name": "google_search",
        "version": "1.0.0",
        "kind": "skill",
        "publisher": "acme",
        "description": "search the web",
    }
    base.update(over)
    return PackageManifest(**base)  # type: ignore[arg-type]


def _package(files: dict[str, bytes] | None = None, **over: object) -> SkillPackage:
    files = files or {"skill.py": b"def run(q):\n    return q\n"}
    manifest = _manifest(**over)
    manifest.content_hash = SkillPackage.hash_files(files)
    return SkillPackage(manifest, files)


class TestSigning:
    def test_sign_and_verify_round_trip(self) -> None:
        kp = generate_keypair()
        payload = b"the package bytes"
        sig = sign(payload, kp.private_b64)
        assert verify(payload, sig, kp.public_b64)

    def test_tampered_payload_fails_verification(self) -> None:
        kp = generate_keypair()
        sig = sign(b"original", kp.private_b64)
        assert not verify(b"tampered", sig, kp.public_b64)

    def test_malformed_signature_is_false_not_error(self) -> None:
        kp = generate_keypair()
        assert verify(b"x", "not-base64!!!", kp.public_b64) is False


class TestPackaging:
    def test_pack_unpack_round_trip(self) -> None:
        pkg = _package()
        blob = pkg.to_bytes()
        loaded = SkillPackage.from_bytes(blob)
        assert loaded.manifest.package_name == "google_search"
        assert loaded.files == pkg.files

    def test_content_hash_detects_tampering(self) -> None:
        pkg = _package()
        assert pkg.verify_content_hash()
        pkg.files["skill.py"] = b"malicious()\n"
        assert not pkg.verify_content_hash()

    def test_zip_slip_path_is_refused(self) -> None:
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", _manifest().to_json())
            zf.writestr("../escape.py", b"evil")
        with pytest.raises(PackageError, match="unsafe path"):
            SkillPackage.from_bytes(buf.getvalue())

    def test_invalid_kind_rejected(self) -> None:
        with pytest.raises(PackageError, match="invalid package kind"):
            _manifest(kind="malware")

    def test_content_hash_is_order_independent(self) -> None:
        a = SkillPackage.hash_files({"x": b"1", "y": b"2"})
        b = SkillPackage.hash_files({"y": b"2", "x": b"1"})
        assert a == b


class TestLocalRegistry:
    async def test_publish_fetch_search(self, tmp_path: Path) -> None:
        reg = LocalDirectoryRegistry(tmp_path)
        await reg.publish(_package())

        hits = await reg.search("search")
        assert [h.package_name for h in hits] == ["google_search"]

        fetched = await reg.fetch("google_search")
        assert fetched.manifest.version == "1.0.0"

    async def test_fetch_missing_raises(self, tmp_path: Path) -> None:
        reg = LocalDirectoryRegistry(tmp_path)
        with pytest.raises(FileNotFoundError):
            await reg.fetch("nope")

    async def test_unparseable_index_degrades_to_an_empty_registry(self, tmp_path: Path) -> None:
        """A corrupt index must not take the registry down with it."""
        reg = LocalDirectoryRegistry(tmp_path)
        (tmp_path / "index.json").write_text("{not json", encoding="utf-8")

        assert await reg.search("anything") == []

    async def test_wrongly_shaped_index_degrades_to_an_empty_registry(
        self, tmp_path: Path
    ) -> None:
        """Valid JSON that is not a list of entries is as corrupt as no JSON.

        Read back as a mapping, the old code iterated its *keys* and failed
        the search with ``AttributeError: 'str' object has no attribute
        'get'`` — a crash in the middle of a lookup, for a file the registry
        had already decided how to treat.
        """
        reg = LocalDirectoryRegistry(tmp_path)
        (tmp_path / "index.json").write_text('{"package_name": "x"}', encoding="utf-8")

        assert await reg.search("x") == []

    async def test_an_entry_missing_a_required_key_degrades_to_an_empty_registry(
        self, tmp_path: Path
    ) -> None:
        """A list of half-written entries is the same failure, not a narrower one.

        ``search`` and ``publish`` read ``package_name`` and ``version`` by
        direct subscript, so an entry without them is not an entry: the old
        code passed the shape check and then died with ``KeyError:
        'version'`` in the middle of a lookup. The whole index is unusable
        once one entry is, so it degrades whole.
        """
        reg = LocalDirectoryRegistry(tmp_path)
        (tmp_path / "index.json").write_text('[{"package_name": "x"}]', encoding="utf-8")

        assert await reg.search("x") == []

    async def test_publishing_replaces_a_malformed_index(self, tmp_path: Path) -> None:
        """The next publish heals the file rather than preserving the damage.

        ``publish`` rewrites the index from what it loaded, so an index that
        degraded to empty is replaced by a clean one — the unusable entry
        does not survive to break the next lookup.
        """
        reg = LocalDirectoryRegistry(tmp_path)
        (tmp_path / "index.json").write_text('[{"package_name": "x"}]', encoding="utf-8")

        await reg.publish(_package())

        entries = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
        assert [e["package_name"] for e in entries] == ["google_search"]

    async def test_the_caller_can_tell_the_three_ways_of_being_empty(
        self, tmp_path: Path
    ) -> None:
        """An empty registry and a broken registry are different failures.

        All three states return no hits, so the log is what tells them apart:
        nothing at all for an index that was never there,
        ``marketplace.index_corrupt`` for one that does not parse, and
        ``marketplace.index_malformed`` for one whose shape is wrong.
        """
        reg = LocalDirectoryRegistry(tmp_path)

        with structlog.testing.capture_logs() as logs:
            assert await reg.search("anything") == []
        assert [entry for entry in logs if entry["event"].startswith("marketplace.index")] == []

        (tmp_path / "index.json").write_text("{not json", encoding="utf-8")
        with structlog.testing.capture_logs() as logs:
            assert await reg.search("anything") == []
        assert [entry["event"] for entry in logs if "marketplace.index" in entry["event"]] == [
            "marketplace.index_corrupt"
        ]

        (tmp_path / "index.json").write_text('{"package_name": "x"}', encoding="utf-8")
        with structlog.testing.capture_logs() as logs:
            assert await reg.search("anything") == []
        assert [entry["event"] for entry in logs if "marketplace.index" in entry["event"]] == [
            "marketplace.index_malformed"
        ]


class TestInstallerGates:
    """Each gate rejects its attack, and rejection leaves zero registry rows."""

    async def _rows(self, db: Database) -> int:
        return await db.fetch_value("SELECT COUNT(*) FROM marketplace_packages") or 0

    async def test_unsigned_package_refused_by_default(self, db: Database) -> None:
        installer = PackageInstaller(db)  # allow_unverified defaults False
        report = await installer.install(_package())
        assert not report.installed
        assert report.refused_at is InstallGate.SIGNATURE
        assert await self._rows(db) == 0

    async def test_unsigned_installs_with_explicit_optin(self, db: Database) -> None:
        installer = PackageInstaller(db, allow_unverified=True)
        report = await installer.install(_package(), mode=PermissionMode.AUTO)
        assert report.installed
        assert await self._rows(db) == 1

    async def test_content_hash_mismatch_refused(self, db: Database) -> None:
        installer = PackageInstaller(db, allow_unverified=True)
        pkg = _package()
        pkg.files["skill.py"] = b"tampered after hashing\n"  # hash no longer matches
        report = await installer.install(pkg, mode=PermissionMode.AUTO)
        assert report.refused_at is InstallGate.CONTENT_HASH
        assert await self._rows(db) == 0

    async def test_ast_scan_gate_refuses_dangerous_code(self, db: Database) -> None:
        from paa.validation.ast_scanner import AstSecurityScanner

        installer = PackageInstaller(db, allow_unverified=True, scanner=AstSecurityScanner())
        pkg = _package(files={"evil.py": b"import os\nos.system('rm -rf /')\n"})
        report = await installer.install(pkg, mode=PermissionMode.AUTO)
        assert report.refused_at is InstallGate.AST_SCAN
        assert await self._rows(db) == 0

    async def test_permission_gate_refuses_overreach(self, db: Database) -> None:
        installer = PackageInstaller(db, allow_unverified=True)
        pkg = _package(required_permissions=["PERM_NET_EGRESS"])
        # LOCKDOWN grants only SANDBOX_RUN.
        report = await installer.install(pkg, mode=PermissionMode.LOCKDOWN)
        assert report.refused_at is InstallGate.PERMISSIONS
        assert await self._rows(db) == 0

    async def test_clean_signed_package_installs(self, db: Database) -> None:
        from paa.validation.ast_scanner import AstSecurityScanner

        installer = PackageInstaller(db, allow_unverified=True, scanner=AstSecurityScanner())
        pkg = _package(files={"skill.py": b"def run(q):\n    return q.upper()\n"})
        report = await installer.install(pkg, mode=PermissionMode.AUTO)
        assert report.installed
        assert InstallGate.AST_SCAN in report.passed_gates
        assert await self._rows(db) == 1

    async def test_uninstall_reverses_install(self, db: Database) -> None:
        installer = PackageInstaller(db, allow_unverified=True)
        await installer.install(_package(), mode=PermissionMode.AUTO)
        assert await installer.uninstall("google_search", "1.0.0") is True
        assert await self._rows(db) == 0

    async def test_trusted_key_signature_path(self, db: Database) -> None:
        """A properly signed package from a trusted key installs as 'valid'
        (only when the signing backend is actually secure)."""
        from paa.marketplace.signing import SignatureState

        if not signing_is_secure():
            pytest.skip("real signing backend not available; dev fallback cannot authenticate")

        kp = generate_keypair()
        pkg = _package()
        pkg.manifest.publisher_key = kp.public_b64
        pkg.manifest.signature = sign(pkg.manifest.signing_bytes(), kp.private_b64)

        installer = PackageInstaller(db)
        await installer.trust_key("k1", kp.public_b64, "acme")
        report = await installer.install(pkg, mode=PermissionMode.AUTO)
        assert report.installed
        assert report.signature_state is SignatureState.VALID
