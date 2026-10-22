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

The untrusted body
------------------

:class:`HttpRegistry` reads a body it did not write, and the ROADMAP calls a
marketplace "the largest attack surface in the design". So the remote gets two
rules, and the rest of this module is their implementation:

1. **The server does not choose the exception type.** Every way the remote can
   let the client down — a status code, a body that is not JSON, a body of the
   wrong shape, an empty body, a body too large to read, a host that cannot be
   reached — raises :class:`RegistryError`, the project's own error. Before
   this rule the remote *did* choose: a malformed body produced a
   ``json.JSONDecodeError``, an ``AttributeError`` or a ``TypeError`` from
   inside this module, and a 500 produced an ``httpx.HTTPStatusError``. A
   caller could not tell a broken server from a broken client, and the fix
   was not "catch more" — it was "the client validates before it parses".

2. **Remote text is data; a log line is a format.** Where remote text may
   appear, and where it may not:

   * A **message** may name a remote value only as a *bounded, repr-quoted*
     fragment — at most 64 characters, escaped so it cannot forge a line. The
     HTTP boundary's messages name none at all: the remote's words live in
     the excerpt, and only there.
   * A **detail** never carries remote text. A detail is written straight
     into a ledger event payload (see :meth:`paa.core.errors.PaaError.
     to_payload`), and the ledger is the project's own record — a remote body
     does not get to write into it.
   * A **log line** carries facts *about* the answer — the status code, the
     reason, the number of bytes read, the number of characters kept — and
     never the answer. A number cannot be a log line, and a fact cannot be
     forged by a server.
   * A **cause** is kept only where its own message obeys the message rule —
     the project's words, or a bounded quoted fragment — and never where it
     would carry the remote's words whole: a ``json.JSONDecodeError`` holds
     the entire document, and a ``BadZipFile`` names a member. Neither is
     chained, so neither travels.
   * The one place remote text is allowed to live is the **excerpt**:
     :attr:`RegistryError.remote_excerpt`, at most
     :data:`_MAX_EXCERPT_CHARS` characters, escaped to a single ASCII line,
     quoted so a reader can see where the remote's words end, and stripped of
     the client's own credential. It is an attribute and not a detail on
     purpose: a caller who wants the server's words has to ask for them, and
     a caller who does not want them cannot have them by accident.
"""

from __future__ import annotations

import abc
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

import httpx
import structlog

from paa.core.errors import PaaError
from paa.marketplace.package import PackageError, SkillPackage

__all__ = [
    "HttpRegistry",
    "LocalDirectoryRegistry",
    "MarketplaceRegistry",
    "RegistryEntry",
    "RegistryError",
]

log = structlog.get_logger(__name__)

#: The keys every index entry must carry, and carry as strings.
#:
#: ``search`` and ``publish`` read these two by direct subscript, so an entry
#: without them is not an entry — and an index holding one is not an index.
#: They are strings because they are the package's identity: ``fetch`` globs
#: the directory on ``package_name`` and the installer gates a package on its
#: name and version.
_REQUIRED_ENTRY_KEYS = ("package_name", "version")

#: The largest search index the client will read off the wire. An index is
#: metadata — a listing, not content — so a megabyte is already a catalogue
#: rather than a result set, and a larger one is refused rather than parsed.
_MAX_INDEX_BYTES = 1024 * 1024

#: The largest package the client will read off the wire. A package is
#: content, so it gets a larger ceiling than the index — and still a ceiling.
#: Without one, a server decides how much memory this process uses.
_MAX_PACKAGE_BYTES = 16 * 1024 * 1024

#: The largest error body the client will read. An error body is only ever
#: read for its excerpt, and an excerpt is a couple of hundred characters, so
#: the rest of the body is not worth the bandwidth.
_MAX_ERROR_BODY_BYTES = 4 * 1024

#: How many characters of remote text the excerpt keeps.
_MAX_EXCERPT_CHARS = 200

#: Every message a :class:`RegistryError` can carry, keyed by reason. The
#: vocabulary is closed on purpose: a message is a format, and a format that
#: is assembled from remote text is a log line the remote can write.
_MESSAGES: dict[str, str] = {
    "unreachable": "could not reach the marketplace registry",
    "http_error": "the marketplace registry returned an error status",
    "body_too_large": "the marketplace registry returned a body larger than this client reads",
    "empty_body": "the marketplace registry returned an empty body",
    "not_json": "the marketplace registry returned a body that is not JSON",
    "unexpected_shape": "the marketplace registry returned a body of an unexpected shape",
    "malformed_package": "the marketplace registry returned a malformed package",
}


#: A sentinel for "this did not parse". A JSON document can legitimately be
#: ``null``, so a parse failure needs a value no JSON document can produce.
_NOT_JSON = object()


def _parse_json(body: bytes) -> Any:
    """The body's JSON value, or :data:`_NOT_JSON` if it is not JSON.

    The decode and the parse fail the same way and are reported the same
    way — "this is not JSON" — because to a caller they are the same
    problem. Neither error is chained into the refusal: a
    ``json.JSONDecodeError`` carries the whole document in its ``doc``
    attribute, and the document is exactly what must not travel with the
    error that describes it.
    """
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return _NOT_JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return _NOT_JSON


def _has_identity(entry: dict[Any, Any]) -> bool:
    """Whether an index entry carries the keys the registry reads directly."""
    return all(isinstance(entry.get(key), str) for key in _REQUIRED_ENTRY_KEYS)


@dataclass(frozen=True, slots=True)
class _Excerpt:
    """The remote text a refusal is allowed to carry.

    ``text`` is the sanitised, bounded text; ``quoted`` is the same text in
    the form it may be shown — surrounded by double quotes, so a reader can
    see where the remote's words end. ``truncated`` says the body was longer
    than what was kept: the truncation is stated, never implied.
    """

    text: str
    truncated: bool

    @property
    def quoted(self) -> str:
        """The excerpt as it may be shown: quoted, or empty when there is none."""
        return f'"{self.text}"' if self.text else ""

    @property
    def chars(self) -> int:
        """How many characters of remote text were kept."""
        return len(self.text)


def _excerpt(raw: bytes, *, secret: str | None = None) -> _Excerpt:
    """The bounded, escaped, quoted excerpt of a remote body.

    This is the one place in the marketplace where the remote's *words* are
    kept, as opposed to a bounded fragment quoted in a message, and it does
    four things to make that safe:

    * it **bounds** the text, at :data:`_MAX_EXCERPT_CHARS` characters, and
      marks the truncation rather than hiding it;
    * it **escapes** the text, so a newline becomes ``\\n`` and any other
      control character becomes a ``\\xNN`` sequence, and the result is a
      single ASCII line — a remote body cannot forge a second log line, move
      a cursor, or make a terminal interpret anything;
    * it **quotes** the text, so a reader can see where the remote's words
      end and the project's begin;
    * it removes the **client's own credential** first, so a server that
      echoes the ``Authorization`` header back cannot turn the excerpt into
      a credential leak.
    """
    text = raw[: _MAX_EXCERPT_CHARS * 4].decode("utf-8", errors="replace")
    if secret:
        text = text.replace(secret, "[redacted]")
    escaped = text.encode("unicode_escape", errors="backslashreplace").decode("ascii")
    # ``unicode_escape`` escapes the backslash and the control characters but
    # leaves the double quote alone, so the quote is escaped here — after
    # the escape pass, so it cannot double-escape a backslash that is
    # already part of an escape sequence.
    escaped = escaped.replace('"', '\\"')
    truncated = len(escaped) > _MAX_EXCERPT_CHARS
    if truncated:
        escaped = escaped[:_MAX_EXCERPT_CHARS] + "..."
    return _Excerpt(escaped, truncated)


@dataclass(frozen=True, slots=True)
class _Remote:
    """What the wire answered: the status, the body prefix, and whether the
    body was longer than the cap the client read it through."""

    status: int
    body: bytes
    truncated: bool


class RegistryError(PaaError):
    """The remote registry's answer could not be used.

    One type for every way a remote registry can fail the client: a status
    code the server chose, a body that is not JSON, a body of the wrong
    shape, an empty body, a body too large to read, a host that could not be
    reached. A caller that wants to know "the registry did not answer
    usefully" catches this and nothing else.

    The ``details`` are facts — the operation, the reason, the status code,
    the number of bytes read, the number of characters kept — and never the
    remote's words, because a detail is written straight into a ledger event
    payload. The remote's words are in :attr:`remote_excerpt`, which is
    bounded, escaped and quoted, and which a caller has to ask for.
    """

    def __init__(
        self,
        message: str,
        *,
        operation: str,
        reason: str,
        status_code: int | None = None,
        body_bytes: int = 0,
        excerpt_chars: int = 0,
        excerpt_truncated: bool = False,
        remote_excerpt: str = "",
        **details: Any,
    ) -> None:
        super().__init__(
            message,
            operation=operation,
            reason=reason,
            status_code=status_code,
            body_bytes=body_bytes,
            excerpt_chars=excerpt_chars,
            excerpt_truncated=excerpt_truncated,
            **details,
        )
        self.operation = operation
        self.reason = reason
        self.status_code = status_code
        self.body_bytes = body_bytes
        self.excerpt_chars = excerpt_chars
        self.excerpt_truncated = excerpt_truncated
        self.remote_excerpt = remote_excerpt


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
        """The index as a list of entries, or an empty list if it is unusable.

        The index is a file on disk that nothing validates on the way in, so
        it is read back as *untrusted*. A file that is valid JSON but not a
        list of usable entries is as unusable as one that does not parse at
        all, and both must degrade to an empty registry rather than fail a
        search in the middle of a lookup — with an ``AttributeError`` about
        ``str.get`` when the file was a mapping, or a ``KeyError`` about
        ``version`` when it was a list of half-written entries.

        Degrading rather than raising is this file's rule rather than an
        accident of convenience. ``search`` returns a list of hits and has no
        error channel, and a registry whose index file is damaged must still
        answer a lookup. The caller is not left guessing which failure it
        was, though, because the three states log three different things:
        *no event at all* for an index that was never there,
        ``marketplace.index_corrupt`` for one that does not parse, and
        ``marketplace.index_malformed`` for one whose shape is wrong. An
        empty registry and a broken registry are different failures, and the
        log is what tells them apart.

        All or nothing: one unusable entry makes the whole index unusable.
        Half an index is a partial absorb, and the house rule for malformed
        input is containment, never partial absorption (see
        ``memory/creator.py``'s "stop on first suspicion"). Adopting the good
        entries and dropping the bad ones would hide the damage until the
        next publish overwrote the file anyway.
        """
        path = self._index_path()
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("marketplace.index_corrupt", path=str(path))
            return []
        if not isinstance(raw, list):
            log.warning("marketplace.index_malformed", path=str(path))
            return []
        entries: list[dict[str, Any]] = []
        for entry in raw:
            # ``isinstance`` narrows the JSON value to an object; JSON object
            # keys are always strings, so an entry that parsed as an object
            # really is a ``dict[str, Any]``.
            if not isinstance(entry, dict) or not _has_identity(entry):
                log.warning("marketplace.index_malformed", path=str(path))
                return []
            entries.append(entry)
        return entries

    def _save_index(self, entries: list[dict[str, Any]]) -> None:
        self._index_path().write_text(json.dumps(entries, indent=2), encoding="utf-8")

    async def search(self, query: str, *, limit: int = 25) -> list[RegistryEntry]:
        q = query.lower().strip()
        hits: list[RegistryEntry] = []
        for entry in self._load_index():
            # ``_load_index`` has already established that ``entry`` carries
            # ``package_name`` and ``version`` as strings, so the subscripts
            # below cannot fail. The remaining fields are optional and are
            # read defensively.
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
    """A remote marketplace over HTTP.

    Publish is a POST; fetch is a GET of the ``.paapkg`` bytes. Everything it
    returns is still routed through the installer's gates — a remote registry is
    exactly the kind of source those gates exist to distrust.

    The remote is untrusted, so this class reads a body it has not validated
    and refuses it in the project's own vocabulary: see the module docstring
    for the two rules, and :class:`RegistryError` for the type every refusal
    uses.

    The ``transport`` is injectable so that the client's *handling of a body*
    can be tested without a socket — the house pattern from ``tests/models``
    (``httpx.MockTransport``). It is the transport and not the client that is
    injected, so the client keeps owning its own lifecycle: every call still
    opens and closes its own ``AsyncClient``.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self._token}"} if self._token else {}

    # -- the wire -----------------------------------------------------------

    async def _exchange(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        cap: int,
        params: dict[str, Any] | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> _Remote:
        """One request, and the bounded prefix of the answer.

        The body is read through ``cap`` and no further: the client never
        buffers a remote body without a ceiling, so a server that keeps
        sending cannot make this process allocate without bound. The read
        stops at the cap and the answer records that it was truncated.

        An error body is read through a much smaller ceiling
        (:data:`_MAX_ERROR_BODY_BYTES`) because it is only ever read for its
        excerpt, and the status code — which is known before the body is
        read — decides which ceiling applies.
        """
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client, client.stream(
                method, url, params=params, content=content, headers=headers
            ) as resp:
                status = resp.status_code
                limit = cap if 200 <= status < 300 else _MAX_ERROR_BODY_BYTES
                chunks: list[bytes] = []
                total = 0
                truncated = False
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > limit:
                        truncated = True
                        break
                    chunks.append(chunk)
                body = b"".join(chunks)
        except httpx.HTTPError as exc:
            # A transport failure is the remote's answer too, and it used to
            # surface as a raw ``httpx`` error the caller had to know about.
            # The exception's *class* is kept (it is not remote text); its
            # message is not, because a server can put text in a reason
            # phrase.
            self._reject(
                operation=operation,
                reason="unreachable",
                excerpt=_Excerpt("", False),
                cause=exc,
                transport_error=type(exc).__name__,
            )
        return _Remote(status=status, body=body, truncated=truncated)

    def _reject(
        self,
        *,
        operation: str,
        reason: str,
        excerpt: _Excerpt,
        status_code: int | None = None,
        body_bytes: int = 0,
        cause: BaseException | None = None,
        **details: Any,
    ) -> NoReturn:
        """Log the refusal in the project's own words, and raise it.

        The log line carries **facts about the answer** and never the answer:
        the operation, the reason, the status code, how many bytes were read
        and how many characters of the server's text were kept. The server's
        text itself is not logged — a remote string is data, and a log line
        is a format.

        The error is raised ``from cause`` — and ``cause`` is ``None`` for
        every refusal about the *body*, so those errors carry no context at
        all. A cause is kept only where it carries the project's own words
        (a transport failure), never where it would carry the remote's.
        """
        log.warning(
            "marketplace.registry_response_rejected",
            operation=operation,
            reason=reason,
            status_code=status_code,
            body_bytes=body_bytes,
            excerpt_chars=excerpt.chars,
        )
        raise RegistryError(
            _MESSAGES[reason],
            operation=operation,
            reason=reason,
            status_code=status_code,
            body_bytes=body_bytes,
            excerpt_chars=excerpt.chars,
            excerpt_truncated=excerpt.truncated,
            remote_excerpt=excerpt.quoted,
            **details,
        ) from cause

    def _reject_body(
        self,
        *,
        operation: str,
        reason: str,
        remote: _Remote,
        cause: BaseException | None = None,
    ) -> NoReturn:
        """Refuse a body the server sent, with its status and its excerpt."""
        self._reject(
            operation=operation,
            reason=reason,
            status_code=remote.status,
            body_bytes=len(remote.body),
            excerpt=_excerpt(remote.body, secret=self._token),
            cause=cause,
        )

    # -- the checks ---------------------------------------------------------

    def _require_success(self, remote: _Remote, *, operation: str) -> None:
        """A status code is the server's to choose, and a failure is a failure.

        ``httpx``'s own ``raise_for_status`` is not usable here for two
        reasons. It raises ``httpx.HTTPStatusError``, whose message carries
        the server's *reason phrase* — server-controlled text, in a message,
        on its way to a log. And it does not raise at all for a redirect, so
        a 302 used to be treated as a success and its body parsed as an
        index. Both are the server choosing the client's behaviour, so the
        check is made here, in the project's own vocabulary.
        """
        if 200 <= remote.status < 300:
            return
        self._reject_body(operation=operation, reason="http_error", remote=remote)

    def _require_body(self, remote: _Remote, *, operation: str) -> bytes:
        """The body, if it is one this client is willing to parse."""
        if remote.truncated:
            self._reject_body(operation=operation, reason="body_too_large", remote=remote)
        if not remote.body:
            self._reject_body(operation=operation, reason="empty_body", remote=remote)
        return remote.body

    def _json(self, body: bytes, *, remote: _Remote, operation: str) -> Any:
        """The parsed body, or a refusal.

        The refusal is raised outside any handler, so the error carries no
        context: the body travels in the bounded excerpt and nowhere else.
        """
        payload = _parse_json(body)
        if payload is _NOT_JSON:
            self._reject_body(operation=operation, reason="not_json", remote=remote)
        return payload

    def _entry(self, raw: Any, *, remote: _Remote, operation: str) -> RegistryEntry:
        """One index entry, checked.

        The old code splatted the entry straight into the dataclass, so the
        *body* chose the exception: a missing key was a ``TypeError`` about
        a missing argument and an extra key a ``TypeError`` about an
        unexpected argument. The shape is checked here instead.

        Unknown keys are ignored, the way the local registry ignores them:
        a server that adds a field to its index has not broken anything, and
        refusing the whole index over it would be a self-inflicted outage.
        """
        if not isinstance(raw, dict):
            self._reject_body(operation=operation, reason="unexpected_shape", remote=remote)
        name = raw.get("package_name")
        version = raw.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            self._reject_body(operation=operation, reason="unexpected_shape", remote=remote)
        kind = raw.get("kind", "skill")
        publisher = raw.get("publisher", "unknown")
        description = raw.get("description", "")
        price = raw.get("price")
        if not isinstance(kind, str) or not isinstance(publisher, str):
            self._reject_body(operation=operation, reason="unexpected_shape", remote=remote)
        if not isinstance(description, str):
            self._reject_body(operation=operation, reason="unexpected_shape", remote=remote)
        if price is not None and not isinstance(price, dict):
            self._reject_body(operation=operation, reason="unexpected_shape", remote=remote)
        return RegistryEntry(
            package_name=name,
            version=version,
            kind=kind,
            publisher=publisher,
            description=description,
            price=price,
        )

    def _entries(self, payload: Any, *, remote: _Remote, operation: str) -> list[RegistryEntry]:
        """The index as entries, or a refusal.

        The old code subscripted the parsed body and iterated whatever came
        out of it, so a body that was a list was an ``AttributeError`` about
        ``get`` and a ``results`` that was a string was a ``TypeError`` about
        ``**``. The shape is checked here instead.
        """
        if not isinstance(payload, dict):
            self._reject_body(operation=operation, reason="unexpected_shape", remote=remote)
        results = payload.get("results", [])
        if not isinstance(results, list):
            self._reject_body(operation=operation, reason="unexpected_shape", remote=remote)
        return [self._entry(raw, remote=remote, operation=operation) for raw in results]

    # -- the operations -----------------------------------------------------

    async def search(self, query: str, *, limit: int = 25) -> list[RegistryEntry]:
        """Search the remote index.

        Unlike the local registry, a malformed answer is a **refusal** and not
        an empty result set. The local registry degrades because its index is
        a local file whose damage the user can fix with one edit; a remote
        registry's answer is the *server's* failure, and a caller that cannot
        tell "the server is broken" from "there are no packages" will act on
        the wrong one.
        """
        remote = await self._exchange(
            "GET",
            f"{self._base}/search",
            operation="search",
            cap=_MAX_INDEX_BYTES,
            params={"q": query, "limit": limit},
            headers=self._headers(),
        )
        self._require_success(remote, operation="search")
        body = self._require_body(remote, operation="search")
        payload = self._json(body, remote=remote, operation="search")
        entries = self._entries(payload, remote=remote, operation="search")
        # The server is asked for ``limit`` results and the client applies it
        # too: a server that ignores the parameter must not decide how much
        # work this process does.
        return entries[: max(limit, 0)]

    async def fetch(self, package_name: str, version: str | None = None) -> SkillPackage:
        path = f"{self._base}/packages/{package_name}"
        if version:
            path += f"/{version}"
        remote = await self._exchange(
            "GET",
            path,
            operation="fetch",
            cap=_MAX_PACKAGE_BYTES,
            headers=self._headers(),
        )
        self._require_success(remote, operation="fetch")
        body = self._require_body(remote, operation="fetch")
        try:
            return SkillPackage.from_bytes(body)
        except PackageError as exc:
            # The package's own error is kept as the cause: its message
            # names at most a bounded, quoted fragment of the manifest — the
            # message rule allows that — and it says *why* the package was
            # refused, which "malformed_package" alone does not. What the
            # server sent is in the excerpt.
            self._reject_body(
                operation="fetch",
                reason="malformed_package",
                remote=remote,
                cause=exc,
            )

    async def publish(self, package: SkillPackage) -> None:
        remote = await self._exchange(
            "POST",
            f"{self._base}/packages",
            operation="publish",
            cap=_MAX_PACKAGE_BYTES,
            content=package.to_bytes(),
            headers={**self._headers(), "content-type": "application/octet-stream"},
        )
        self._require_success(remote, operation="publish")
        # A successful publish has no body to parse, so an empty one is fine.
        # An *error* body is read for its excerpt and refused above.
