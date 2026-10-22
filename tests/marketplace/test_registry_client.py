"""The remote body is untrusted input, and this is where it stops.

``HttpRegistry`` fetches a body it did not write. Before this suite the
**server chose the local exception type**: a body that was not JSON raised
``json.JSONDecodeError``, a body that was a list raised ``AttributeError``
about ``get``, a ``results`` that was a string raised ``TypeError`` about
``**``, an entry that was missing a key raised ``TypeError`` about a missing
argument, and a 500 raised ``httpx.HTTPStatusError`` whose message carries
the server's own *reason phrase*. A caller could not tell a broken server
from a broken client, and the server's text rode into the exception's message
on its way to a log line.

So the suite pins three things, and every case here is one of them:

* **one error type** — every way the remote can fail the client raises
  :class:`~paa.marketplace.registry_client.RegistryError`, the project's own
  error, and never a ``TypeError``/``AttributeError``/``JSONDecodeError``
  from inside the client;
* **bounded text** — the server's text survives as a bounded, escaped,
  quoted excerpt, and the truncation is visible rather than implied;
* **no unescaped remote text in a log** — the refusal is logged with facts
  about the answer and never with the answer, and the test shows exactly
  what does reach the log.

The HTTP call is real-ish and the socket is not: the client is handed an
``httpx.MockTransport`` (the house pattern from ``tests/models``), so the
thing under test is the client's *handling of a body*, and nothing here
reaches the network.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import AsyncIterator, Callable

import httpx
import pytest
import structlog.testing

from paa.core.errors import PaaError
from paa.marketplace.package import PackageError, PackageManifest, SkillPackage
from paa.marketplace.registry_client import HttpRegistry, RegistryError

Responder = Callable[[httpx.Request], httpx.Response]

#: The messages a refusal may carry. Written out here rather than imported,
#: so that a change to the vocabulary is a change to *this* list: the
#: messages are the project's own, and a remote body may not add to them.
_MESSAGES = {
    "could not reach the marketplace registry",
    "the marketplace registry returned an error status",
    "the marketplace registry returned a body larger than this client reads",
    "the marketplace registry returned an empty body",
    "the marketplace registry returned a body that is not JSON",
    "the marketplace registry returned a body of an unexpected shape",
    "the marketplace registry returned a malformed package",
}

#: A body that tries to speak for itself: a forged log line, a fake error and
#: a credential-shaped string, all in one.
_HOSTILE = "forged\n[error] user=admin token=sk-remote-abcdef0123456789"


def _always(response: httpx.Response) -> Responder:
    """A responder that answers every request with ``response``."""

    def responder(_request: httpx.Request) -> httpx.Response:
        return response

    return responder


def _registry(responder: Responder, *, token: str | None = None) -> HttpRegistry:
    """A registry whose every request is served by ``responder``.

    The transport is the seam, not the client: the client under test is the
    one that reads and validates the body, and the socket is not what is
    being tested.
    """
    return HttpRegistry(
        "https://registry.test",
        transport=httpx.MockTransport(responder),
        token=token,
    )


class _CountingBody:
    """A body produced lazily, in pieces, counting what was produced.

    A socket delivers a body in pieces. Handing the client one giant buffer
    would test the transport's memory behaviour instead of the client's, so
    the body is produced a chunk at a time — and the count says how much of
    it the client actually read.
    """

    def __init__(self, total: int, *, chunk: int = 64 * 1024) -> None:
        self.total = total
        self.chunk = chunk
        self.produced = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        remaining = self.total
        while remaining > 0:
            size = min(self.chunk, remaining)
            self.produced += 1
            yield b"x" * size
            remaining -= size


def _package_bytes(manifest: bytes) -> bytes:
    """A ``.paapkg`` whose manifest is exactly ``manifest``."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("manifest.json", manifest)
    return buffer.getvalue()


def _valid_manifest() -> dict[str, str]:
    """A manifest the loader accepts."""
    return {
        "package_name": "google_search",
        "version": "1.0.0",
        "kind": "skill",
        "publisher": "acme",
        "description": "search the web",
    }


def _valid_package_bytes() -> bytes:
    """A ``.paapkg`` the loader accepts."""
    return _package_bytes(json.dumps(_valid_manifest()).encode("utf-8"))


def _bomb_package_bytes() -> bytes:
    """A small ``.paapkg`` that unpacks into a very large one.

    Sixty-four megabytes of zeros is a few kilobytes of deflate, so the
    body on the wire is tiny and the payload is not. It is written in
    pieces so that the *test* does not allocate sixty-four megabytes to
    prove the loader refuses to.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(_valid_manifest()).encode("utf-8"))
        with zf.open("bomb.bin", "w") as member:
            remaining = 64 * 1024 * 1024
            while remaining > 0:
                size = min(1024 * 1024, remaining)
                member.write(b"\0" * size)
                remaining -= size
    return buffer.getvalue()


def _skill_package() -> SkillPackage:
    """A package to publish."""
    manifest = PackageManifest(
        package_name="google_search",
        version="1.0.0",
        kind="skill",
        publisher="acme",
        description="search the web",
    )
    files = {"skill.py": b"def run(q):\n    return q\n"}
    manifest.content_hash = SkillPackage.hash_files(files)
    return SkillPackage(manifest, files)


class TestTheBodyIsRefusedInTheProjectsVoice:
    """Every malformed shape is the same failure, in the project's words."""

    async def test_a_body_that_is_not_json_is_the_projects_own_error(self) -> None:
        """The first shape the old code fell over on.

        The old code called ``resp.json()`` on whatever arrived, so a body
        that was not JSON raised ``json.JSONDecodeError`` — a bare
        ``ValueError`` from the standard library, raised from inside the
        client, where a caller had no reason to expect it.
        """
        registry = _registry(_always(httpx.Response(200, text="<html>hello</html>")))

        with pytest.raises(RegistryError) as excinfo:
            await registry.search("q")

        assert excinfo.value.reason == "not_json"
        assert excinfo.value.message == "the marketplace registry returned a body that is not JSON"
        assert isinstance(excinfo.value, PaaError)
        # The body does not travel with the error that describes it: a
        # ``json.JSONDecodeError`` holds the whole document in its ``doc``
        # attribute, so it is not chained, and neither is anything else.
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__context__ is None

    async def test_a_json_body_that_is_not_an_object_is_the_projects_own_error(self) -> None:
        """A body that is a list used to be an ``AttributeError`` about ``get``.

        The old code subscripted the parsed body without checking that it
        was an object, so a perfectly valid JSON array failed the search
        with ``AttributeError: 'list' object has no attribute 'get'`` — an
        error from inside the client, about a body the server sent.
        """
        registry = _registry(_always(httpx.Response(200, json=[1, 2, 3])))

        with pytest.raises(RegistryError) as excinfo:
            await registry.search("q")

        assert excinfo.value.reason == "unexpected_shape"

    async def test_a_results_field_that_is_not_a_list_is_the_projects_own_error(self) -> None:
        """``results`` as a string used to be a ``TypeError`` about ``**``.

        The old code did ``RegistryEntry(**e)`` for every element of
        whatever ``results`` held, so a string was splatted character by
        character and the search died with ``TypeError: argument after **
        must be a mapping, not str``.
        """
        registry = _registry(_always(httpx.Response(200, json={"results": "abc"})))

        with pytest.raises(RegistryError) as excinfo:
            await registry.search("q")

        assert excinfo.value.reason == "unexpected_shape"

    async def test_a_results_field_that_is_a_mapping_is_the_projects_own_error(self) -> None:
        """A mapping is not a list either, and it used to fail the same way."""
        registry = _registry(_always(httpx.Response(200, json={"results": {"a": 1}})))

        with pytest.raises(RegistryError) as excinfo:
            await registry.search("q")

        assert excinfo.value.reason == "unexpected_shape"

    async def test_a_results_field_that_is_an_empty_thing_is_the_projects_own_error(self) -> None:
        """An empty ``results`` is still not a list.

        The empty string and the empty mapping are the shapes that make the
        distinction visible: iterated, they yield nothing at all, so without
        the check on ``results`` itself they would be read as an index with
        no entries — a search that quietly answers "nothing here" when the
        server actually sent nonsense.
        """
        empty_results: list[object] = ["", {}]
        for value in empty_results:
            registry = _registry(_always(httpx.Response(200, json={"results": value})))

            with pytest.raises(RegistryError) as excinfo:
                await registry.search("q")

            assert excinfo.value.reason == "unexpected_shape"

    async def test_an_entry_without_an_identity_is_the_projects_own_error(self) -> None:
        """An entry missing its name used to be a ``TypeError`` about an argument.

        The old code splatted the entry into the dataclass, so an entry
        without ``package_name`` and ``version`` failed with ``TypeError:
        RegistryEntry.__init__() missing 3 required positional arguments``.
        """
        registry = _registry(_always(httpx.Response(200, json={"results": [{"kind": "skill"}]})))

        with pytest.raises(RegistryError) as excinfo:
            await registry.search("q")

        assert excinfo.value.reason == "unexpected_shape"

    async def test_an_entry_with_a_wrongly_typed_field_is_the_projects_own_error(self) -> None:
        """A name that is not a string is not an entry.

        The old code passed the value straight into the dataclass, and a
        dataclass does not check types at runtime: the search succeeded and
        handed back an entry whose ``package_name`` was an integer.
        """
        registry = _registry(
            _always(httpx.Response(200, json={"results": [{"package_name": 1, "version": "1.0"}]}))
        )

        with pytest.raises(RegistryError) as excinfo:
            await registry.search("q")

        assert excinfo.value.reason == "unexpected_shape"

    async def test_an_empty_body_is_the_projects_own_error(self) -> None:
        """An empty body is not an empty index.

        The old code parsed it and failed with ``JSONDecodeError``; a caller
        reading that could not tell "the registry is empty" from "the
        registry is broken", and an empty answer to a search is a perfectly
        ordinary thing.
        """
        registry = _registry(_always(httpx.Response(200, content=b"")))

        with pytest.raises(RegistryError) as excinfo:
            await registry.search("q")

        assert excinfo.value.reason == "empty_body"
        assert excinfo.value.message == "the marketplace registry returned an empty body"

    async def test_an_enormous_body_is_refused_before_it_is_parsed(self) -> None:
        """The client reads through a ceiling, and stops reading at it.

        A server that keeps sending must not decide how much memory this
        process uses. The body is delivered in pieces, the way a socket
        delivers it, and the count of what was produced says the client
        stopped short of the end.
        """
        body = _CountingBody(4 * 1024 * 1024)
        registry = _registry(_always(httpx.Response(200, content=body)))

        with pytest.raises(RegistryError) as excinfo:
            await registry.search("q")

        assert excinfo.value.reason == "body_too_large"
        # The client read its ceiling and no more: the body was four
        # megabytes and the ceiling is one.
        assert excinfo.value.details["body_bytes"] == 1024 * 1024
        assert body.produced < 4 * 1024 * 1024 // body.chunk

    async def test_every_malformed_shape_raises_the_same_error_type(self) -> None:
        """One type for every shape — the whole point of the change.

        Each case is a body the old code answered with a different
        exception from a different library. The caller now has one thing to
        catch, and it is the project's own.
        """
        shapes: list[httpx.Response] = [
            httpx.Response(200, text="<html>hello</html>"),
            httpx.Response(200, json=[1, 2, 3]),
            httpx.Response(200, json={"results": "abc"}),
            httpx.Response(200, json={"results": [{"kind": "skill"}]}),
            httpx.Response(200, json={"results": [{"package_name": 1, "version": "1.0"}]}),
            httpx.Response(200, content=b""),
            httpx.Response(500, json={"results": []}),
            httpx.Response(404, text="no such package"),
        ]

        for response in shapes:
            registry = _registry(_always(response))
            with pytest.raises(RegistryError) as excinfo:
                await registry.search("q")
            assert type(excinfo.value) is RegistryError
            assert isinstance(excinfo.value, PaaError)
            assert excinfo.value.message in _MESSAGES

    async def test_the_status_code_is_the_servers_and_a_500_is_still_a_failure(self) -> None:
        """A 500 with a perfectly good JSON body is still a failure.

        The status code is the server's to choose, and it is checked before
        the body is looked at: a valid body behind a 500 is a server that
        failed, not a result set.
        """
        registry = _registry(_always(httpx.Response(500, json={"results": []})))

        with pytest.raises(RegistryError) as excinfo:
            await registry.search("q")

        assert excinfo.value.reason == "http_error"
        assert excinfo.value.status_code == 500
        assert excinfo.value.message == "the marketplace registry returned an error status"

    async def test_a_redirect_is_a_failure_and_not_an_answer(self) -> None:
        """A 302 is not an index.

        ``httpx``'s own ``raise_for_status`` does not raise for a redirect,
        so the old code treated a 302 as a success and parsed whatever the
        redirect carried as an index — a body this client has no business
        accepting.
        """
        registry = _registry(
            _always(
                httpx.Response(
                    302,
                    headers={"location": "https://elsewhere.test/index"},
                    json={"results": []},
                )
            )
        )

        with pytest.raises(RegistryError) as excinfo:
            await registry.search("q")

        assert excinfo.value.reason == "http_error"
        assert excinfo.value.status_code == 302

    async def test_an_unreachable_registry_is_the_projects_own_error(self) -> None:
        """A transport failure is the remote's answer too.

        It used to surface as a raw ``httpx.ConnectError``, so a caller had
        to know about ``httpx`` to handle a registry that was down. The
        class of the transport error is kept — it is the project's own fact,
        not the server's words — and the refusal is the project's own error.
        """

        def boom(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        registry = _registry(boom)

        with pytest.raises(RegistryError) as excinfo:
            await registry.search("q")

        assert excinfo.value.reason == "unreachable"
        assert excinfo.value.details["transport_error"] == "ConnectError"
        assert isinstance(excinfo.value.__cause__, httpx.ConnectError)


class TestTheFetchedPackageIsRefusedTheSameWay:
    """The package's own parser reads the same untrusted body."""

    async def test_a_body_that_is_not_a_package_is_the_projects_own_error(self) -> None:
        """Bytes that are not a zip used to be a ``TypeError``.

        ``SkillPackage.from_bytes`` caught ``zipfile.BadZipFile`` and
        nothing else, so a body that was not a zip at all reached
        ``io.BytesIO`` and failed with ``TypeError: expected bytes-like
        object``. The refusal is now the registry's own.
        """
        registry = _registry(_always(httpx.Response(200, content=b"not a zip at all")))

        with pytest.raises(RegistryError) as excinfo:
            await registry.fetch("pkg")

        assert excinfo.value.reason == "malformed_package"
        assert excinfo.value.message == "the marketplace registry returned a malformed package"
        # The package's own error is the cause: it says *why* the package
        # was refused, and its message obeys the message rule.
        assert isinstance(excinfo.value.__cause__, PackageError)

    async def test_a_manifest_that_is_not_json_is_the_projects_own_error(self) -> None:
        """A manifest that does not parse used to be a ``JSONDecodeError``.

        The manifest is read out of the archive the server sent, so it is
        the same untrusted input, and it used to escape as a
        ``json.JSONDecodeError`` from inside the package loader.
        """
        registry = _registry(
            _always(httpx.Response(200, content=_package_bytes(b"not json at all")))
        )

        with pytest.raises(RegistryError) as excinfo:
            await registry.fetch("pkg")

        assert excinfo.value.reason == "malformed_package"

    async def test_a_manifest_that_is_not_an_object_is_the_projects_own_error(self) -> None:
        """A manifest that is a JSON array used to be an ``AttributeError``."""
        registry = _registry(_always(httpx.Response(200, content=_package_bytes(b"[1,2,3]"))))

        with pytest.raises(RegistryError) as excinfo:
            await registry.fetch("pkg")

        assert excinfo.value.reason == "malformed_package"

    async def test_a_manifest_with_a_wrongly_typed_field_is_the_projects_own_error(self) -> None:
        """A manifest whose name is not a string is not a manifest.

        The loader used to hand the values straight to the dataclass, and a
        dataclass does not check types at runtime: the package was accepted
        with an integer for a name, and the installer's gates were left to
        decide what to do with it.
        """
        manifest = {
            "package_name": 5,
            "version": "1.0.0",
            "kind": "skill",
            "publisher": "acme",
        }
        registry = _registry(
            _always(httpx.Response(200, content=_package_bytes(json.dumps(manifest).encode())))
        )

        with pytest.raises(RegistryError) as excinfo:
            await registry.fetch("pkg")

        assert excinfo.value.reason == "malformed_package"

    async def test_a_package_that_unpacks_to_more_than_the_loader_accepts_is_refused(self) -> None:
        """A small body can unpack into a very large one.

        The bytes on the wire are capped, but a zip is a *compressed* body:
        sixty-four megabytes of zeros is a few kilobytes of deflate. The
        loader reads the sizes the archive declares — which are exactly what
        ``zipfile`` will hand back — and refuses the package before it
        unpacks a byte of it.
        """
        registry = _registry(_always(httpx.Response(200, content=_bomb_package_bytes())))

        with pytest.raises(RegistryError) as excinfo:
            await registry.fetch("pkg")

        assert excinfo.value.reason == "malformed_package"

    async def test_an_empty_package_body_is_the_projects_own_error(self) -> None:
        """An empty body is refused before the loader is asked to parse it."""
        registry = _registry(_always(httpx.Response(200, content=b"")))

        with pytest.raises(RegistryError) as excinfo:
            await registry.fetch("pkg")

        assert excinfo.value.reason == "empty_body"

    async def test_an_enormous_package_body_is_refused_before_it_is_parsed(self) -> None:
        """The package path has a ceiling too, and it is a larger one.

        A package is content rather than metadata, so it gets sixteen
        megabytes where the index gets one — and no more than that.
        """
        body = _CountingBody(17 * 1024 * 1024)
        registry = _registry(_always(httpx.Response(200, content=body)))

        with pytest.raises(RegistryError) as excinfo:
            await registry.fetch("pkg")

        assert excinfo.value.reason == "body_too_large"
        assert excinfo.value.details["body_bytes"] == 16 * 1024 * 1024
        assert body.produced < 17 * 1024 * 1024 // body.chunk

    async def test_a_valid_package_is_still_fetched(self) -> None:
        """The happy path is untouched: the fix refuses, it does not break."""
        registry = _registry(_always(httpx.Response(200, content=_valid_package_bytes())))

        package = await registry.fetch("google_search")

        assert package.manifest.package_name == "google_search"
        assert package.manifest.version == "1.0.0"


class TestThePublishPathIsRefusedTheSameWay:
    """A publish that fails is a failure in the project's own voice."""

    async def test_a_500_is_the_projects_own_error(self) -> None:
        """The status is the server's, and it decides the outcome."""
        registry = _registry(_always(httpx.Response(500, text="the registry is unwell")))

        with pytest.raises(RegistryError) as excinfo:
            await registry.publish(_skill_package())

        assert excinfo.value.reason == "http_error"
        assert excinfo.value.status_code == 500
        assert excinfo.value.operation == "publish"

    async def test_a_successful_publish_with_an_empty_body_is_not_a_failure(self) -> None:
        """A 204 has no body, and no body is not a problem for a publish.

        There is nothing to parse in a successful publish, so the empty-body
        refusal — which is about a body the client was going to *read* — does
        not apply.
        """
        registry = _registry(_always(httpx.Response(204)))

        with structlog.testing.capture_logs() as logs:
            await registry.publish(_skill_package())

        # No refusal was logged: an empty body is only a problem where a
        # body was going to be read.
        assert [entry for entry in logs if "marketplace.registry" in entry["event"]] == []


class TestTheServersTextIsBounded:
    """The remote's words survive, but only bounded, escaped and quoted."""

    async def test_the_error_body_is_kept_as_a_quoted_excerpt(self) -> None:
        """The server's text is allowed in exactly one place.

        It is the excerpt: quoted, so a reader can see where the remote's
        words end, and it is on the exception rather than in the message.
        """
        registry = _registry(_always(httpx.Response(404, text="no such package")))

        with pytest.raises(RegistryError) as excinfo:
            await registry.search("q")

        assert excinfo.value.remote_excerpt == '"no such package"'
        # The message is the project's own; the remote's words are not in it.
        assert "no such package" not in excinfo.value.message
        # And they are not in the details either: a detail is written
        # straight into a ledger event payload.
        assert "no such package" not in str(excinfo.value.details)
        assert "no such package" not in str(excinfo.value)

    async def test_a_long_error_body_is_truncated_and_the_truncation_is_visible(self) -> None:
        """Bounded means bounded, and the bound is visible.

        Five hundred characters of the server's own words come back as two
        hundred of them plus the marker that says there were more — the
        truncation is stated, never implied.
        """
        registry = _registry(_always(httpx.Response(500, text="A" * 500)))

        with pytest.raises(RegistryError) as excinfo:
            await registry.search("q")

        assert excinfo.value.remote_excerpt == '"' + "A" * 200 + '..."'
        assert excinfo.value.excerpt_truncated is True
        assert excinfo.value.details["excerpt_chars"] == 203
        assert excinfo.value.details["excerpt_truncated"] is True

    async def test_the_excerpt_cannot_forge_a_line(self) -> None:
        """The excerpt is one escaped line, whatever the server sent.

        A newline in the body would end the excerpt and start a new log
        line; a control character would move a cursor. Both are escaped, so
        the excerpt is a single ASCII line.
        """
        registry = _registry(_always(httpx.Response(500, text="first\nsecond\rthird")))

        with pytest.raises(RegistryError) as excinfo:
            await registry.search("q")

        excerpt = excinfo.value.remote_excerpt
        assert "\n" not in excerpt
        assert "\r" not in excerpt
        assert excerpt == '"first\\nsecond\\rthird"'

    async def test_the_clients_own_credential_is_not_echoed_back(self) -> None:
        """A server that echoes the token does not get a credential leak.

        The client knows its own token, so the token is removed from the
        excerpt before anything else happens to it.
        """
        token = "sk-registry-0123456789abcdef"
        registry = _registry(
            _always(httpx.Response(500, text=f"your token {token} was rejected")),
            token=token,
        )

        with pytest.raises(RegistryError) as excinfo:
            await registry.search("q")

        assert token not in excinfo.value.remote_excerpt
        assert "[redacted]" in excinfo.value.remote_excerpt
        assert token not in str(excinfo.value)


class TestTheServersTextNeverReachesALog:
    """A remote string is data; a log line is a format."""

    async def test_the_refusal_is_logged_with_facts_and_not_with_the_answer(self) -> None:
        """The test shows exactly what does reach the log.

        The line carries the operation, the reason, the status code, the
        number of bytes read and the number of characters kept. The server's
        own words are not in it, and the key set is pinned so that a field
        carrying them cannot be added quietly.
        """
        registry = _registry(_always(httpx.Response(500, text=_HOSTILE)))

        with (
            structlog.testing.capture_logs() as logs,
            pytest.raises(RegistryError) as excinfo,
        ):
            await registry.search("q")

        entries = [
            entry
            for entry in logs
            if entry["event"] == "marketplace.registry_response_rejected"
        ]
        assert len(entries) == 1
        assert set(entries[0]) == {
            "event",
            "log_level",
            "operation",
            "reason",
            "status_code",
            "body_bytes",
            "excerpt_chars",
        }
        assert entries[0]["operation"] == "search"
        assert entries[0]["reason"] == "http_error"
        assert entries[0]["status_code"] == 500
        # The server's words are in none of the log records, and not in the
        # error's message or details either.
        for record in logs:
            assert "forged" not in str(record)
            assert "sk-remote-abcdef0123456789" not in str(record)
        assert "forged" not in excinfo.value.message
        assert "forged" not in str(excinfo.value.details)

    async def test_a_hostile_body_cannot_add_a_log_line(self) -> None:
        """One refusal is one log line, whatever the body says.

        The body tried to write a line of its own. It is escaped in the
        excerpt and it is not in the log, so the log still has exactly the
        lines the client wrote.
        """
        registry = _registry(_always(httpx.Response(500, text=_HOSTILE)))

        with (
            structlog.testing.capture_logs() as logs,
            pytest.raises(RegistryError),
        ):
            await registry.search("q")

        assert len(logs) == 1
        assert logs[0]["event"] == "marketplace.registry_response_rejected"

    async def test_a_successful_search_logs_nothing(self) -> None:
        """The client is not chatty about an answer it accepted."""
        registry = _registry(
            _always(
                httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "package_name": "google_search",
                                "version": "1.0.0",
                                "kind": "skill",
                                "publisher": "acme",
                            }
                        ]
                    },
                )
            )
        )

        with structlog.testing.capture_logs() as logs:
            hits = await registry.search("search")

        assert [hit.package_name for hit in hits] == ["google_search"]
        assert logs == []


class TestTheIndexIsStillAnIndex:
    """The shape the client accepts, and the shape it hands back."""

    async def test_a_valid_index_is_parsed(self) -> None:
        """The happy path."""
        registry = _registry(
            _always(
                httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "package_name": "google_search",
                                "version": "1.0.0",
                                "kind": "skill",
                                "publisher": "acme",
                                "description": "search the web",
                                "price": {"usd": 0},
                            }
                        ]
                    },
                )
            )
        )

        hits = await registry.search("search")

        assert len(hits) == 1
        assert hits[0].package_name == "google_search"
        assert hits[0].price == {"usd": 0}

    async def test_an_unknown_key_in_an_entry_is_ignored(self) -> None:
        """A server that adds a field has not broken anything.

        The old code splatted the entry into the dataclass, so an unknown
        key was a ``TypeError`` about an unexpected argument and the whole
        index was lost. The local registry ignores unknown keys, and so does
        this one.
        """
        registry = _registry(
            _always(
                httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "package_name": "google_search",
                                "version": "1.0.0",
                                "kind": "skill",
                                "publisher": "acme",
                                "surprise": [1, 2, 3],
                            }
                        ]
                    },
                )
            )
        )

        hits = await registry.search("search")

        assert [hit.package_name for hit in hits] == ["google_search"]

    async def test_the_limit_is_applied_by_the_client_too(self) -> None:
        """A server that ignores the limit does not decide the work.

        The limit is a parameter of the call, so the client applies it as
        well as asking for it.
        """
        registry = _registry(
            _always(
                httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "package_name": f"pkg{index}",
                                "version": "1.0",
                                "kind": "skill",
                                "publisher": "p",
                            }
                            for index in range(5)
                        ]
                    },
                )
            )
        )

        hits = await registry.search("pkg", limit=2)

        assert [hit.package_name for hit in hits] == ["pkg0", "pkg1"]
