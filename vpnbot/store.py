import json
import math
import secrets
import sqlite3
import time
from pathlib import Path


class ActionExpired(Exception):
    pass


class Store:
    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS chats (
                user_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL,
                reachable INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS actions (
                token TEXT PRIMARY KEY, actor INTEGER NOT NULL, kind TEXT NOT NULL,
                payload TEXT NOT NULL, expires REAL NOT NULL, consumed INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY, actor INTEGER NOT NULL, body TEXT NOT NULL,
                created REAL NOT NULL, finished INTEGER DEFAULT 0, reported INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                ready_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(job_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY, at REAL NOT NULL, actor INTEGER NOT NULL,
                operation TEXT NOT NULL, detail TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cooldown (
                id INTEGER PRIMARY KEY CHECK(id=1), until_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS updates (
                update_id INTEGER PRIMARY KEY, processed_at REAL NOT NULL
            );
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    def recover(self):
        # A crash after Telegram accepted a message has an unknowable outcome.
        with self.db:
            self.db.execute("UPDATE deliveries SET state='unknown' WHERE state='sending'")
            self.db.execute("DELETE FROM actions WHERE expires < ?", (time.time(),))
            self.db.execute(
                "DELETE FROM jobs WHERE finished=1 AND reported=1 AND created < ?",
                (time.time() - 30 * 86400,),
            )

    def register(self, user_id: int, chat_id: int):
        with self.db:
            self.db.execute(
                "INSERT INTO chats VALUES (?,?,1) ON CONFLICT(user_id) DO UPDATE "
                "SET chat_id=excluded.chat_id, reachable=1",
                (user_id, chat_id),
            )

    def reachable(self, user_id: int):
        return self.db.execute(
            "SELECT chat_id FROM chats WHERE user_id=? AND reachable=1",
            (user_id,),
        ).fetchone()

    def recipients(self, allowed: set[int]) -> list[int]:
        return [
            r[0]
            for r in self.db.execute("SELECT user_id FROM chats WHERE reachable=1")
            if r[0] in allowed
        ]

    def blocked(self, user_id: int):
        with self.db:
            self.db.execute("UPDATE chats SET reachable=0 WHERE user_id=?", (user_id,))

    def action(self, actor: int, kind: str, payload: dict, ttl: int = 600) -> str:
        token = secrets.token_urlsafe(12)
        with self.db:
            self.db.execute("DELETE FROM actions WHERE expires < ?", (time.time(),))
            self.db.execute(
                "INSERT INTO actions VALUES (?,?,?,?,?,0)",
                (
                    token,
                    actor,
                    kind,
                    json.dumps(payload),
                    time.time() + ttl,
                ),
            )
        return token

    def read_action(self, token: str, actor: int, kind: str) -> dict:
        row = self.db.execute(
            "SELECT * FROM actions WHERE token=? AND actor=? AND kind=? "
            "AND consumed=0 AND expires>?",
            (token, actor, kind, time.time()),
        ).fetchone()
        if row is None:
            raise ActionExpired("Кнопка устарела или уже использована. Повторите команду")
        return json.loads(row["payload"])

    def consume(self, token: str, actor: int, kind: str) -> dict:
        with self.db:
            payload = self.read_action(token, actor, kind)
            changed = self.db.execute(
                "UPDATE actions SET consumed=1 WHERE token=? AND consumed=0",
                (token,),
            ).rowcount
            if changed != 1:
                raise ActionExpired("Кнопка уже использована")
        return payload

    def enqueue(self, token: str, actor: int, recipients: list[int]) -> int:
        # Consume confirmation and create the durable queue in the same transaction.
        with self.db:
            data = self.read_action(token, actor, "send")
            changed = self.db.execute(
                "UPDATE actions SET consumed=1 WHERE token=? AND consumed=0",
                (token,),
            ).rowcount
            if changed != 1:
                raise ActionExpired("Кнопка уже использована")
            cursor = self.db.execute(
                "INSERT INTO jobs(actor,body,created) VALUES (?,?,?)",
                (actor, data["body"], time.time()),
            )
            job = cursor.lastrowid
            self.db.executemany(
                "INSERT INTO deliveries(job_id,user_id) VALUES (?,?)",
                [(job, user_id) for user_id in sorted(set(recipients))],
            )
            self.db.execute(
                "INSERT INTO audit(at,actor,operation,detail) VALUES (?,?,?,?)",
                (
                    time.time(),
                    actor,
                    "broadcast",
                    json.dumps({"job": job, "count": len(recipients)}),
                ),
            )
        return job

    def next_delivery(self):
        if self.cooling_down():
            return None
        return self.db.execute(
            "SELECT d.job_id,d.user_id,j.body FROM deliveries d JOIN jobs j ON j.id=d.job_id "
            "WHERE d.state='pending' AND d.ready_at<=? ORDER BY d.job_id,d.user_id LIMIT 1",
            (time.time(),),
        ).fetchone()

    def cooling_down(self) -> bool:
        row = self.db.execute("SELECT until_at FROM cooldown WHERE id=1").fetchone()
        return bool(row and row[0] > time.time())

    def cooldown(self, delay: float):
        with self.db:
            self.db.execute(
                "INSERT INTO cooldown VALUES (1,?) ON CONFLICT(id) DO UPDATE "
                "SET until_at=MAX(until_at,excluded.until_at)",
                (time.time() + delay,),
            )

    def delivery(self, job: int, user: int, state: str, delay: float = 0):
        with self.db:
            self.db.execute(
                "UPDATE deliveries SET state=?,ready_at=? WHERE job_id=? AND user_id=?",
                (state, time.time() + delay, job, user),
            )

    def finish(self):
        with self.db:
            self.db.execute(
                "UPDATE jobs SET finished=1 WHERE finished=0 AND NOT EXISTS "
                "(SELECT 1 FROM deliveries WHERE job_id=jobs.id "
                "AND state IN ('pending','sending'))",
            )
        return self.db.execute(
            "SELECT id,actor FROM jobs WHERE finished=1 AND reported=0"
        ).fetchall()

    def counts(self, job: int) -> dict[str, int]:
        return dict(
            self.db.execute(
                "SELECT state,COUNT(*) FROM deliveries WHERE job_id=? GROUP BY state",
                (job,),
            ).fetchall()
        )

    def reported(self, job: int):
        with self.db:
            self.db.execute("UPDATE jobs SET reported=1 WHERE id=?", (job,))

    def recent_jobs(self):
        return self.db.execute(
            "SELECT id,finished FROM jobs ORDER BY id DESC LIMIT 10",
        ).fetchall()

    def update_done(self, update_id: int) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM updates WHERE update_id=?",
                (update_id,),
            ).fetchone()
            is not None
        )

    def mark_update(self, update_id: int):
        with self.db:
            self.db.execute(
                "INSERT INTO updates VALUES (?,?) ON CONFLICT DO NOTHING", (update_id, time.time())
            )
            self.db.execute(
                "DELETE FROM updates WHERE processed_at < ?", (time.time() - 7 * 86400,)
            )

    def continuation_delay(self) -> int | None:
        pending = self.db.execute(
            "SELECT MIN(ready_at) FROM deliveries WHERE state='pending'",
        ).fetchone()[0]
        reports = self.db.execute("SELECT 1 FROM jobs WHERE reported=0 LIMIT 1").fetchone()
        if pending is None and not reports:
            return None
        ready = time.time() if pending is None else pending
        cooldown = self.db.execute("SELECT until_at FROM cooldown WHERE id=1").fetchone()
        if cooldown:
            ready = max(ready, cooldown[0])
        return max(1, math.ceil(ready - time.time()))

    def audit(self, actor: int, operation: str, **detail):
        with self.db:
            self.db.execute(
                "INSERT INTO audit(at,actor,operation,detail) VALUES (?,?,?,?)",
                (
                    time.time(),
                    actor,
                    operation,
                    json.dumps(detail),
                ),
            )
