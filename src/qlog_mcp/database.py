"""Read-only SQLite connection management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from .errors import DatabaseNotConfiguredError, DatabaseNotFoundError


class Database:
    def __init__(self, path: Path | None) -> None:
        self.path = path

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        """Open the configured QLog database in SQLite read-only mode."""
        if self.path is None:
            raise DatabaseNotConfiguredError(
                "QLog database was not configured; use --database or QLOG_DB_PATH"
            )
        if not self.path.is_file():
            raise DatabaseNotFoundError(f"QLog database does not exist: {self.path}")

        uri = self.path.resolve().as_uri() + "?mode=ro"
        async with aiosqlite.connect(uri, uri=True) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("PRAGMA query_only = ON")
            yield connection
