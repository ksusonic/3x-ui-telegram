"""PostgreSQL storage with a bounded cross-deployment processing lease."""

import contextlib
import secrets
import socket
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict

from .store import Store

# Vercel functions stop after 300 seconds; leave a margin before another owner can start.
LEASE_SECONDS = 330


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
        host = options.get("host", "")
        if host and not host.startswith("/") and "hostaddr" not in options:
            # Supabase session poolers support IPv4; avoid unreliable IPv6 paths.
            addresses = socket.getaddrinfo(
                host,
                int(options.get("port", 5432)),
                socket.AF_INET,
                socket.SOCK_STREAM,
            )
            options["hostaddr"] = addresses[0][4][0]
        self.raw = psycopg.connect(**options, autocommit=True, prepare_threshold=None)
        self.raw.execute("SET statement_timeout = '10s'")
        self.raw.execute("SET idle_in_transaction_session_timeout = '30s'")
        self.raw.execute("SET idle_session_timeout = '320s'")
        self.db = Connection(self.raw)
        self.locked = False
        self.owner = secrets.token_urlsafe(24)

    def try_lock(self) -> bool:
        if self.locked:
            return True
        if self.raw.execute("SELECT to_regclass('vpn_bot.work_lock')").fetchone()[0] is None:
            self.raw.execute("CREATE SCHEMA IF NOT EXISTS vpn_bot")
            self.raw.execute(
                "CREATE TABLE IF NOT EXISTS vpn_bot.work_lock ("
                "id BIGINT PRIMARY KEY CHECK(id=1),owner TEXT NOT NULL,"
                "expires_at TIMESTAMPTZ NOT NULL)",
            )
            self.raw.execute("ALTER TABLE vpn_bot.work_lock ENABLE ROW LEVEL SECURITY")
        row = self.raw.execute(
            "INSERT INTO vpn_bot.work_lock AS l VALUES "
            "(1,%s,clock_timestamp() + make_interval(secs => %s)) "
            "ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at "
            "WHERE l.expires_at<=clock_timestamp() RETURNING owner",
            (self.owner, LEASE_SECONDS),
        ).fetchone()
        self.locked = bool(row and row[0] == self.owner)
        return self.locked

    def initialize(self):
        if not self.locked:
            raise RuntimeError("Storage lock required")
        self.raw.execute("SET search_path TO vpn_bot")
        secured = self.raw.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='vpn_bot' AND c.relkind='r' AND c.relrowsecurity",
        ).fetchall()
        required = {
            "chats",
            "actions",
            "jobs",
            "deliveries",
            "audit",
            "cooldown",
            "updates",
            "work_lock",
        }
        if required <= {row[0] for row in secured}:
            return
        schema = Path(__file__).with_name("schema.sql").read_text()
        with self.raw.transaction():
            for statement in schema.split(";"):
                if statement.strip():
                    self.raw.execute(statement)

    def close(self):
        try:
            if self.locked and not self.raw.closed:
                with contextlib.suppress(psycopg.Error):
                    self.raw.execute(
                        "DELETE FROM vpn_bot.work_lock WHERE id=1 AND owner=%s", (self.owner,)
                    )
        finally:
            self.raw.close()
