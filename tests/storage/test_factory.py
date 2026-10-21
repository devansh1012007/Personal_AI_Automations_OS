"""The relational backend factory — which substrate a configuration selects.

Why this file exists
--------------------
``get_database`` is the one place where the substrate is *chosen*.
Everything downstream of it — the ledger, the repositories, the queue — is
written against a single contract and never asks which engine it is
holding, so the factory is the only place where a mistake in reading the
configuration can put the whole runtime on the wrong substrate: a ledger
written by the embedded backend into a server deployment, or a server
topology that quietly opens a local file.

The factory accepts two inputs — the whole
:class:`~paa.config.Settings` and the storage sub-model alone
(:class:`~paa.config.StorageSettings`) — because most callers hold only
the sub-model. The two are told apart by *kind*, and the distinction is
load-bearing in both directions: the whole object carries the answers
inside its ``storage`` field, and the sub-model *is* the answer. Handing
either one the other is a mistake, and the two tests below would each
catch it.

What each test is about
-----------------------
* the two inputs select the **same** backend, so which settings object a
  caller holds is not a decision the caller has to make;
* the server backend is selected by configuration and by nothing else;
* a server backend with **no DSN** is refused, by name, rather than built
  and left to fail at the first query;
* the factory builds a backend and does not **open** one — the caller owns
  the lifecycle, and a factory that connected would be a factory that
  could not be called on a machine with no server.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paa.config import Settings, StorageSettings
from paa.core.errors import StorageError
from paa.storage.relational import get_database
from paa.storage.relational.database import Database
from paa.storage.relational.postgres import PostgresDatabase

_DSN = "postgresql://paa:paa@127.0.0.1:5432/paa"


def test_the_whole_settings_object_selects_the_embedded_backend(tmp_path: Path) -> None:
    """The whole object carries the answers inside its ``storage`` field.

    The composition root holds a whole ``Settings`` and hands it over
    as-is, so this is the path the runtime actually takes.
    """
    sqlite_path = tmp_path / "state" / "paa.db"
    settings = Settings(storage=StorageSettings(sqlite_path=sqlite_path))

    db = get_database(settings)

    assert isinstance(db, Database)
    assert db.path == sqlite_path


def test_the_storage_sub_model_alone_selects_the_same_backend(tmp_path: Path) -> None:
    """Most callers hold only the sub-model, and it has to be enough.

    The sub-model is the answer itself: it carries the backend selection,
    the path and the DSN. A factory that wanted the whole object would
    send every such caller to a refusal, and a factory that wanted the
    sub-model would send every whole-object caller to the wrong place.
    """
    storage = StorageSettings(sqlite_path=tmp_path / "state" / "paa.db")

    db = get_database(storage)

    assert isinstance(db, Database)
    assert db.path == storage.sqlite_path


def test_the_two_inputs_agree(tmp_path: Path) -> None:
    """Which settings object a caller holds is not its decision to make.

    The two are the same backend, from the same configuration — so a
    caller that holds the sub-model and a caller that holds the whole
    object cannot drift apart, and the composition root cannot end up on
    a different substrate from the rest of the runtime.
    """
    settings = Settings(storage=StorageSettings(sqlite_path=tmp_path / "state" / "paa.db"))

    from_whole = get_database(settings)
    from_sub_model = get_database(settings.storage)

    assert isinstance(from_whole, Database)
    assert isinstance(from_sub_model, Database)
    assert from_whole.path == from_sub_model.path


def test_the_server_backend_is_selected_by_configuration() -> None:
    """The substrate is chosen by the setting, and by nothing else.

    The DSN is carried through untouched: the factory hands back an
    *unconnected* backend, and a backend that had rewritten the DSN would
    connect somewhere else than the operator configured.
    """
    storage = StorageSettings(backend_relational="postgres", postgres_dsn=_DSN)

    db = get_database(storage)

    assert isinstance(db, PostgresDatabase)
    assert db.dsn == _DSN


def test_a_server_backend_without_a_dsn_is_refused_by_name() -> None:
    """A missing DSN is a configuration error, and it says which one.

    Building the backend anyway would move the failure to the first
    query, where it reads as a connection fault rather than as the
    setting that was never filled in.
    """
    storage = StorageSettings(backend_relational="postgres", postgres_dsn=None)

    with pytest.raises(StorageError) as excinfo:
        get_database(storage)

    assert excinfo.value.substrate == "postgres"
    assert "PAA_STORAGE__POSTGRES_DSN" in str(excinfo.value)


def test_the_factory_builds_a_backend_and_does_not_open_one(tmp_path: Path) -> None:
    """The caller owns the lifecycle.

    A factory that connected would be a factory that could not be called
    on a machine with no server, and one that created the file would be a
    factory that had done work the caller is going to do again. Both
    would be invisible in the return value — a connected backend and an
    unconnected one are the same object — so the only place the
    difference shows is the filesystem.
    """
    sqlite_path = tmp_path / "state" / "paa.db"

    db = get_database(StorageSettings(sqlite_path=sqlite_path))

    assert isinstance(db, Database)
    assert not sqlite_path.exists()
    assert not sqlite_path.parent.exists()
