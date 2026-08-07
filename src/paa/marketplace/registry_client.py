"""Marketplace registries — where packages are searched, fetched, and published.

Two backends behind one interface, so the rest of the system does not care
which is in use:

* **LocalDirectoryRegistry** — a folder of ``.paapkg`` files with an
  ``index.json``. Needs no network and is the default, keeping the local-first
  promise: you can run a private marketplace off a directory.
* **HttpRegistry** — a remote index over HTTP for a shared/public marketplace.

Neither installs anything. Fetching returns bytes; the :class:`~paa.marketplace.
installer.PackageInstaller` is the only thing that decides whether those bytes
become a live skill. Keeping fetch and install separate is deliberate — a
registry is untrusted, so nothing it returns is trusted until the installer's
gates have run.
"""

from __future__ import annotations

import abc
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from paa.marketplace.package import SkillPackage

__all__ = ["HttpRegistry", "LocalDirectoryRegistry", "MarketplaceRegistry", "RegistryEntry"]

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class RegistryEntry:
    """A package's public listing, before download."""

    package_name: str
    version: str
    kind: str
    publisher: str
    description: str = ""
    price: dict[str, Any] | None = None


class MarketplaceRegistry(abc.ABC):
    """Search / fetch / publish. Implementations are untrusted sources."""

    @abc.abstractmethod
    async def search(self, query: str, *, limit: int = 25) -> list[RegistryEntry]: ...

    @abc.abstractmethod
    async def fetch(self, package_name: str, version: str | None = None) -> SkillPackage: ...

    @abc.abstractmethod
    async def publish(self, package: SkillPackage) -> None: ...


class LocalDirectoryRegistry(MarketplaceRegistry):
    """A marketplace backed by a local directory of ``.paapkg`` files."""

    def __init__(self, directory: Path | str) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _index_path(self) -> Path:
        return self._dir / "index.json"

    def _load_index(self) -> list[dict[str, Any]]:
        path = self._index_path()
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("marketplace.index_corrupt", path=str(path))
            return []

    def _save_index(self, entries: list[dict[str, Any]]) -> None:
        self._index_path().write_text(json.dumps(entries, indent=2), encoding="utf-8")

    async def search(self, query: str, *, limit: int = 25) -> list[RegistryEntry]:
        q = query.lower().strip()
        hits: list[RegistryEntry] = []
        for entry in self._load_index():
            haystack = f"{entry.get('package_name','')} {entry.get('description','')}".lower()
            if not q or q in haystack:
                hits.append(
                    RegistryEntry(
                        package_name=entry["package_name"],
                        version=entry["version"],
                        kind=entry.get("kind", "skill"),
                        publisher=entry.get("publisher", "unknown"),
                        description=entry.get("description", ""),
                        price=entry.get("price"),
                    )
                )
        return hits[:limit]

    async def fetch(self, package_name: str, version: str | None = None) -> SkillPackage:
        candidates = sorted(self._dir.glob(f"{package_name}-*.paapkg"))
        if version is not None:
            candidates = [c for c in candidates if c.stem.endswith(version)]
        if not candidates:
            raise FileNotFoundError(f"no package {package_name!r} (version={version}) in registry")
        return SkillPackage.load(candidates[-1])

    async def publish(self, package: SkillPackage) -> None:
        m = package.manifest
        filename = f"{m.package_name}-{m.version}.paapkg"
        package.write(self._dir / filename)
        index = self._load_index()
        index = [
            e
            for e in index
            if not (e["package_name"] == m.package_name and e["version"] == m.version)
        ]
        index.append(
            {
                "package_name": m.package_name,
                "version": m.version,
                "kind": m.kind,
                "publisher": m.publisher,
                "description": m.description,
                "price": m.price,
                "file": filename,
            }
        )
        self._save_index(index)
        log.info("marketplace.published", package=m.package_name, version=m.version)


class HttpRegistry(MarketplaceRegistry):
    """A remote marketplace over HTTP. Lazily uses httpx.

    Publish is a POST; fetch is a GET of the ``.paapkg`` bytes. Everything it
    returns is still routed through the installer's gates — a remote registry is
    exactly the kind of source those gates exist to distrust.
    """

    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = 30.0) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self._token}"} if self._token else {}

    async def search(self, query: str, *, limit: int = 25) -> list[RegistryEntry]:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(
                f"{self._base}/search",
                params={"q": query, "limit": limit},
                headers=self._headers(),
            )
            resp.raise_for_status()
            return [RegistryEntry(**e) for e in resp.json().get("results", [])]

    async def fetch(self, package_name: str, version: str | None = None) -> SkillPackage:
        import httpx

        path = f"{self._base}/packages/{package_name}"
        if version:
            path += f"/{version}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(path, headers=self._headers())
            resp.raise_for_status()
            return SkillPackage.from_bytes(resp.content)

    async def publish(self, package: SkillPackage) -> None:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self._base}/packages",
                content=package.to_bytes(),
                headers={**self._headers(), "content-type": "application/octet-stream"},
            )
            resp.raise_for_status()
