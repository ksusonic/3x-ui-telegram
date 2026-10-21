"""PostgreSQL storage; use a direct connection or a session pooler, not transaction pooling."""

import contextlib
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict

from .store import Store

# Serialize Telegram updates and delivery work across all functions/deployments.
LOCK_ID = 818228639991


class Row(tuple):
    def __new__(cls, names, values):
        row = super().__new__(cls, values)
        row.names = names
        return row

    def __getitem__(self, key):
        return super().__getitem__(self.names.index(key) if isinstance(key, str) else key)


class Cursor:
    def __init__(self, cursor, returning_id=False):
        self.cursor = cursor
        self.rowcount = cursor.rowcount
        self.lastrowid = cursor.fetchone()[0] if returning_id else None

    def _row(self, value):
        if value is None:
            return None
        return Row([c.name for c in self.cursor.description], value)

    def fetchone(self):
        return self._row(self.cursor.fetchone())

    def fetchall(self):
        return [self._row(row) for row in self.cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class Connection:
    """Small parameter/row adapter for the shared Store SQL (no SQL from user input)."""

    def __init__(self, raw):
        self.raw = raw

    @staticmethod
    def sql(statement):
        return statement.replace("?", "%s").replace(
            "MAX(until_at,excluded.until_at)",
            "GREATEST(cooldown.until_at,excluded.until_at)",
        )

    def execute(self, statement, params=()):
        returning = statement.startswith("INSERT INTO jobs(")
        if returning:
            statement += " RETURNING id"
        return Cursor(self.raw.execute(self.sql(statement), params), returning)

    def executemany(self, statement, params):
        cursor = self.raw.cursor()
        cursor.executemany(self.sql(statement), params)
        return Cursor(cursor)

    def __enter__(self):
        self.transaction = self.raw.transaction()
        self.transaction.__enter__()
        return self

    def __exit__(self, *args):
        return self.transaction.__exit__(*args)

    def close(self):
        self.raw.close()


class PostgresStore(Store):
    def __init__(self, url: str, *, require_tls=True):
        options = conninfo_to_dict(url)
        if require_tls:
            if options.get("sslmode", "require") not in {"require", "verify-ca", "verify-full"}:
                raise ValueError("DATABASE_URL must enable TLS")
            options.setdefault("sslmode", "require")
        options.setdefault("connect_timeout", 10)
        self.raw = psycopg.connect(**options, autocommit=True, prepare_threshold=None)
        self.raw.execute("SET statement_timeout = '10s'")
        self.db = Connection(self.raw)
        self.locked = False

    def try_lock(self) -> bool:
        if self.locked:
            return True
        self.locked = self.raw.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_ID,)).fetchone()[0]
        return self.locked

    def initialize(self):
        if not self.locked:
            raise RuntimeError("Storage lock required")
        schema = Path(__file__).with_name("schema.sql").read_text()
        with self.raw.transaction():
            for statement in schema.split(";"):
                if statement.strip():
                    self.raw.execute(statement)

    def close(self):
        try:
            if self.locked and not self.raw.closed:
                with contextlib.suppress(psycopg.Error):
                    self.raw.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
        finally:
            self.raw.close()
