"""The relational contract — what a repository is allowed to assume.

ADR-0019 rule 1 says the two relational substrates "present the exact async
surface" of each other. Until now that was a claim in a document and nothing
else: the composition root hardcoded the SQLite class, so the PostgreSQL
class was never type-checked against anything, and the two had drifted —
``transaction()`` yielded a different kind of object, a column's value
arrived in a different Python type, and a failure arrived as a different
exception class.

This module is the contract, written down. Both backends satisfy it, and
:meth:`paa.runtime.Runtime.build` holds one of them behind it, so a
repository that is written against this protocol runs on either engine
without knowing which.

The contract is deliberately small: it is the set of operations the
repository layer actually performs, not a wish list. A member that no
repository calls does not belong here — the earlier draft of this contract
carried a ``vacuum()`` that PostgreSQL could not honour without running
outside a transaction, and a ``path`` property that has no meaning on a
server substrate.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, runtime_checkable

from paa.storage.relational.connection import TransactionConnection
from paa.storage.relational.dialect import Dialect

__all__ = ["RelationalBackend"]


@runtime_checkable
class RelationalBackend(Protocol):
    """The operations every repository performs against the relational store."""

    @property
    def dialect(self) -> Dialect:
        """The engine's dialect — the SQL fragments with no portable spelling."""
        ...

    async def connect(self) -> None:
        """Open the substrate and apply the schema. Idempotent."""
        ...

    async def close(self) -> None:
        """Close the substrate and release its resources."""
        ...

    def transaction(self) -> AbstractAsyncContextManager[TransactionConnection]:
        """An all-or-nothing write transaction.

        Used as an async context manager. The object it yields is the
        *common* transaction connection, not the driver's — so a repository
        never imports a driver, and never has to know which engine it holds.
        """
        ...

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[Any]:
        """Every row the statement returns."""
        ...

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> Any | None:
        """The first row the statement returns, or ``None``."""
        ...

    async def fetch_value(self, sql: str, params: Sequence[Any] = ()) -> Any:
        """The first column of the first row, or ``None``."""
        ...

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run one write statement in its own transaction. Returns rowcount."""
        ...

    async def execute_many(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        """Run one write statement for every row of ``rows``."""
        ...

    async def table_names(self) -> list[str]:
        """Every table in the substrate, as the substrate names them.

        Asked of the backend rather than written as a query, because the
        catalog is spelled differently on each engine and a caller that
        hard-codes one spelling is a caller that breaks on the other.
        """
        ...
