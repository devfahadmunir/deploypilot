"""
Database layer for the Acme MCP server.

- One shared connection pool (connections are reused, not reopened per call).
- query():             read-only transactions, enforced by PostgreSQL itself.
- write_transaction(): all-or-nothing writes; anything that fails rolls back.
- audit():             records every write in the same transaction as the write.
"""

import atexit
import logging
import os
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

import psycopg
from dotenv import load_dotenv
from mcp.server.mcpserver.exceptions import ToolError
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool, PoolTimeout

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://acme:acme@localhost:5432/acme")

log = logging.getLogger("acme-mcp.db")
_pool: ConnectionPool | None = None

DB_UNAVAILABLE = "The Acme database is unavailable right now. Please try again shortly."


def get_pool() -> ConnectionPool:
    """Create the pool lazily, on first use."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            DATABASE_URL,
            min_size=1,
            max_size=5,
            timeout=5,  # seconds to wait for a free connection
            kwargs={"row_factory": dict_row, "connect_timeout": 5},
            open=True,
        )
        # Close connections cleanly when the program exits, instead of leaving
        # the pool's background threads to be killed during interpreter shutdown.
        atexit.register(_pool.close)
    return _pool


def clean(value: Any) -> Any:
    """Convert DB types (Decimal, datetime) into JSON-friendly values."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def query(sql: str, params: tuple | dict = ()) -> list[dict]:
    """Run a query inside a READ ONLY transaction and return rows as dicts.
    PostgreSQL rejects any INSERT/UPDATE/DELETE here, even if one slips in."""
    try:
        with get_pool().connection() as conn:
            with conn.transaction():
                conn.execute("SET TRANSACTION READ ONLY")
                rows = conn.execute(sql, params).fetchall()
        return clean(rows)
    except (psycopg.OperationalError, PoolTimeout) as exc:
        log.error("Database unavailable: %s", exc)
        raise ToolError(DB_UNAVAILABLE) from exc


@contextmanager
def write_transaction() -> Iterator[psycopg.Connection]:
    """All-or-nothing write. If anything inside raises (including ToolError),
    every change in the block is rolled back."""
    try:
        with get_pool().connection() as conn:
            with conn.transaction():
                yield conn
    except (psycopg.OperationalError, PoolTimeout) as exc:
        log.error("Database unavailable: %s", exc)
        raise ToolError(DB_UNAVAILABLE) from exc


def audit(conn: psycopg.Connection, tool: str, arguments: dict, result: dict) -> None:
    """Record a write. Runs on the same connection/transaction as the write,
    so a write can never succeed without its audit entry (or vice versa)."""
    conn.execute(
        "INSERT INTO audit_log (tool, arguments, result) VALUES (%s, %s, %s)",
        (tool, Jsonb(clean(arguments)), Jsonb(clean(result))),
    )
