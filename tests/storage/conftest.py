"""Shared storage-layer fixtures."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from paa.storage.coldlake.minio_backend import S3BlobStore
from paa.storage.relational.database import Database
from paa.storage.relational.postgres import PostgresDatabase

#: The PostgreSQL server the server-backed tests run against, or ``None``.
#:
#: The environment variable is the house convention for a test-only switch
#: (``PAA_TEST_VERBOSE`` is the existing example). It names the *server*,
#: and the tests below make a database of their own on it — so a run can
#: never write to the database a deployment is using, whatever the DSN
#: points at.
#:
#: Without it, the server-backed tests skip: the default suite has to stay
#: runnable on a laptop with no PostgreSQL, and a test that fails because
#: there is no server is a test that fails for the wrong reason.
POSTGRES_DSN = os.environ.get("PAA_TEST_POSTGRES_DSN")


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    """A connected, schema-applied database on a throwaway file."""
    database = Database(tmp_path / "paa.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
async def postgres_dsn() -> AsyncIterator[str]:
    """A DSN for a database of the test's own, on the configured server.

    The database is created before the test and dropped after it, so a run
    never writes to the database the deployment is using — the DSN names
    the *server*, and the tests make their own database on it.
    """
    if POSTGRES_DSN is None:
        pytest.skip("needs PAA_TEST_POSTGRES_DSN")
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    info = conninfo_to_dict(POSTGRES_DSN)
    admin = make_conninfo(**{**info, "dbname": "postgres"})
    name = f"paa_storage_test_{os.getpid()}_{uuid.uuid4().hex[:8]}"

    connection = await psycopg.AsyncConnection.connect(admin, autocommit=True)
    async with connection:
        await connection.execute(f'CREATE DATABASE "{name}"')
    try:
        yield make_conninfo(**{**info, "dbname": name})
    finally:
        connection = await psycopg.AsyncConnection.connect(admin, autocommit=True)
        async with connection:
            await connection.execute(f'DROP DATABASE IF EXISTS "{name}"')


@pytest.fixture
async def postgres(postgres_dsn: str) -> AsyncIterator[PostgresDatabase]:
    """A connected database on the throwaway server, with the schema applied."""
    database = PostgresDatabase(postgres_dsn)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


# ---------------------------------------------------------------------------
# The live S3 servers
# ---------------------------------------------------------------------------
#
# The convention is the PostgreSQL one, exactly: the environment names a
# **server**, and the fixture makes a resource of its own on it. So a run can
# never write to the bucket a deployment is using, whatever the endpoint points
# at, and two runs on the same server never collide.
#
#     PAA_TEST_S3_ENDPOINT       the server to run against
#     PAA_TEST_S3_ACCESS_KEY     its access key
#     PAA_TEST_S3_SECRET_KEY     its secret key
#     PAA_TEST_S3_BUCKET         optional; the base name of the test's bucket
#
# A **second** server is named the same way with a label in front of the last
# word, so the same four names describe any number of implementations:
#
#     PAA_TEST_S3_SEAWEEDFS_ENDPOINT, PAA_TEST_S3_SEAWEEDFS_ACCESS_KEY, ...
#
# The unlabelled names are the label-less member of the same rule, so the
# house convention is the single-server case of it rather than a second thing
# to remember. The label is what a failing test id names, which is the whole
# reason for it: a live suite that runs against two implementations has to say
# which one it failed on.
#
# The bucket is never *taken* from the environment: `PAA_TEST_S3_BUCKET` names
# the **base** of the test's bucket, and the fixture appends the pid and a
# uuid, so a run can never write to a bucket the operator chose. The result
# is `paa-s3-test_{pid}_{uuid8}` — hyphens, because S3's own naming rules
# forbid underscores and both servers enforce them — and it is dropped in
# teardown.
#
# The exact `docker run` recipe for both servers is in the docstring of
# `tests/storage/test_s3_live.py`, which is where the suite that uses this
# fixture lives.

#: The prefix every S3 test variable shares.
_S3_PREFIX = "PAA_TEST_S3_"

#: The suffix that names a server's endpoint — and therefore the variable
#: whose presence means "there is a server by this label".
_S3_ENDPOINT_SUFFIX = "ENDPOINT"

#: The base name of a test's bucket, before the pid and the uuid.
#:
#: Hyphens, not underscores. S3's own bucket-naming rules allow lowercase
#: letters, digits, hyphens and periods, and **not** underscores — and
#: **both** servers enforce them: a name carrying an underscore is refused
#: with ``InvalidBucketName`` before the bucket exists, on SeaweedFS and on
#: RustFS alike (measured; see ADR-0033). The name is therefore spelled the
#: way S3's own rules spell it, so the suite runs against either
#: implementation without a special case.
_S3_BUCKET_BASE = "paa-s3-test"


def _s3_var(label: str, name: str) -> str:
    """The variable that carries ``name`` for the server called ``label``.

    The empty label is the house convention — ``PAA_TEST_S3_ENDPOINT`` — and a
    labelled one is the same name with the label in front of the last word,
    ``PAA_TEST_S3_SEAWEEDFS_ENDPOINT``. One rule, and the unlabelled spelling
    is a member of it rather than a separate convention.
    """
    return f"{_S3_PREFIX}{label}_{name}" if label else f"{_S3_PREFIX}{name}"


@dataclass(frozen=True)
class S3Server:
    """One S3 server the live suite can run against.

    It carries the server's *coordinates* and nothing else: no bucket, no
    client. The bucket belongs to the test that asked for it, and the clients
    are built per test so that a test can make a second one with different
    credentials — which is how the "wrong credentials" failure mode is
    provoked at all.
    """

    label: str
    endpoint: str
    access_key: str
    secret_key: str
    bucket_base: str

    @property
    def title(self) -> str:
        """The name this server goes by in a test id."""
        return self.label or "default"

    def bucket_name(self) -> str:
        """A bucket name no other run and no other test can be using.

        The pid and the uuid make it unique within a host and within a
        process. The separators are hyphens because S3's own naming rules
        forbid underscores and **both** servers enforce them — a name
        carrying an underscore is refused with ``InvalidBucketName`` on
        SeaweedFS and on RustFS alike (measured; ADR-0033).
        """
        return f"{self.bucket_base}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def s3_servers() -> list[S3Server]:
    """Every S3 server the environment names, in a stable order.

    A server is a server when its endpoint is named. The credentials default
    to the **empty string** rather than to "unset", and that is deliberate:
    an unset credential would let boto3 resolve one from the developer's own
    environment or ``~/.aws/credentials``, so a test pointed at a server
    could authenticate with a key the operator never named. The empty
    string is a *named* credential that no server accepts — both servers
    answer it with ``InvalidAccessKeyId`` (SeaweedFS) and
    ``UnauthorizedAccess`` (RustFS), and the store reports the refusal as
    ``AccessDenied``, which is the right answer for "the server said no".

    A server that does not authenticate at all accepts the empty credential,
    which is why it is the empty string and not a missing one.
    """
    labels = [""]
    labels.extend(
        sorted(
            name[len(_S3_PREFIX) : -len(f"_{_S3_ENDPOINT_SUFFIX}")]
            for name in os.environ
            if name.startswith(_S3_PREFIX)
            and name.endswith(f"_{_S3_ENDPOINT_SUFFIX}")
            and len(name) > len(_S3_PREFIX) + len(_S3_ENDPOINT_SUFFIX)
        )
    )

    servers: list[S3Server] = []
    for label in labels:
        endpoint = os.environ.get(_s3_var(label, _S3_ENDPOINT_SUFFIX))
        if not endpoint:
            continue
        servers.append(
            S3Server(
                label=label,
                endpoint=endpoint,
                access_key=os.environ.get(_s3_var(label, "ACCESS_KEY"), ""),
                secret_key=os.environ.get(_s3_var(label, "SECRET_KEY"), ""),
                bucket_base=os.environ.get(_s3_var(label, "BUCKET"), _S3_BUCKET_BASE),
            )
        )
    return servers


@dataclass
class S3Target:
    """A live server, a bucket of this run's own, and the two clients.

    Two clients on purpose. ``store`` is the store under test, which builds
    its own client the way the deployment builds it; ``raw`` is the test's
    own, and it is the only way to look at the bucket without going through
    the store. The ``BlobStore`` contract has no list operation (ADR-0032
    records the decision), so the assertions that need to see the raw object
    use this one.
    """

    server: S3Server
    bucket: str
    store: S3BlobStore
    raw: Any
    #: Buckets this test made beyond the fixture's own. A server that
    #: *creates* the bucket a write names leaves one behind, and a suite that
    #: leaves buckets behind is a suite that fills the server up.
    extra_buckets: list[str] = field(default_factory=list)

    def absent_store(self) -> S3BlobStore:
        """A store bound to a bucket that is not there.

        The name is recorded so the fixture can drop the bucket if the
        server created it — SeaweedFS does, on a write, and RustFS does not.
        """
        name = f"{self.server.bucket_base}-absent-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.extra_buckets.append(name)
        return S3BlobStore(
            name,
            endpoint_url=self.server.endpoint,
            access_key=self.server.access_key,
            secret_key=self.server.secret_key,
        )


def _delete_objects(client: Any, bucket: str) -> None:
    """Delete every object in the bucket, and leave the bucket.

    The listing is paginated so a bucket with more objects than one page
    holds is emptied too.
    """
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            client.delete_object(Bucket=bucket, Key=obj["Key"])


def _drop_bucket(client: Any, bucket: str) -> None:
    """Empty the bucket and drop it.

    The objects go first because a bucket that still holds them cannot be
    dropped, and a leftover bucket is a leftover bucket.
    """
    _delete_objects(client, bucket)
    client.delete_bucket(Bucket=bucket)


def drop_bucket_if_present(client: Any, bucket: str) -> None:
    """Drop a bucket and its objects, if the server has it at all.

    A bucket that is not there is the ordinary case and is not an error, so
    it is answered by doing nothing. The driver's answer is read
    *structurally* — the same way the store reads it — so the driver itself
    is never imported: a response that says "not there" in its code or in
    its status means there is nothing to drop, and anything else is raised,
    because a connection that failed is not an absence.
    """
    try:
        client.head_bucket(Bucket=bucket)
    except Exception as exc:
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            error = response.get("Error")
            metadata = response.get("ResponseMetadata")
            code = error.get("Code") if isinstance(error, dict) else None
            status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
            if code in ("404", "NoSuchBucket") or status == 404:
                return
        raise
    _drop_bucket(client, bucket)


@pytest.fixture(scope="session")
def s3_bucket(request: pytest.FixtureRequest) -> Iterator[S3Target]:
    """A live S3 server with a bucket of this run's own.

    The fixture is **indirectly** parametrised: the server arrives as
    ``request.param``, so the same test body runs against every server the
    environment names and the failing case's id says which one it was.

    **The bucket is per *run*, not per test.** That is not a convenience: a
    SeaweedFS server maps every bucket onto a filer collection with a volume
    of its own, and it cannot allocate one for an unbounded number of
    buckets — the volume server runs out, and every write then fails with a
    ``500 InternalError`` that names no writable volumes. A suite that made
    a bucket per test exhausted the server in a dozen tests. One bucket per
    run is one collection, which is what the deployment uses too, and it is
    emptied between tests so that each test still starts from an empty
    namespace.

    The bucket is created here and dropped in teardown, so a run leaves
    nothing behind on the server it was pointed at: not a bucket, not an
    object, not a key. The server itself is not the fixture's to create or to
    destroy — it is named by the environment, exactly as the PostgreSQL
    server is — so a run never starts, stops or reconfigures a server that
    something else may be using.
    """
    server: S3Server = request.param
    boto3 = pytest.importorskip("boto3", reason="needs the s3 extra (paa[s3])")

    raw = boto3.client(
        "s3",
        endpoint_url=server.endpoint,
        aws_access_key_id=server.access_key,
        aws_secret_access_key=server.secret_key,
        region_name="us-east-1",
    )
    bucket = server.bucket_name()
    raw.create_bucket(Bucket=bucket)
    store = S3BlobStore(
        bucket,
        endpoint_url=server.endpoint,
        access_key=server.access_key,
        secret_key=server.secret_key,
    )
    try:
        yield S3Target(server=server, bucket=bucket, store=store, raw=raw)
    finally:
        try:
            _drop_bucket(raw, bucket)
        finally:
            store.close()
            raw.close()


@pytest.fixture
def s3_target(s3_bucket: S3Target) -> Iterator[S3Target]:
    """The run's bucket, emptied so that this test starts from nothing.

    The bucket is the run's, so it has to be emptied between tests: the
    assertions that count the objects in it would otherwise see what the
    previous test left behind, and a test that passes because of what
    another test did is not a test.
    """
    _delete_objects(s3_bucket.raw, s3_bucket.bucket)
    try:
        yield s3_bucket
    finally:
        for name in s3_bucket.extra_buckets:
            drop_bucket_if_present(s3_bucket.raw, name)
        s3_bucket.extra_buckets.clear()
