"""SQLite persistence layer (aiosqlite): guild settings, strikes, layout backups."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER NOT NULL,
    key      TEXT NOT NULL,
    value    TEXT NOT NULL,
    PRIMARY KEY (guild_id, key)
);
CREATE TABLE IF NOT EXISTS strikes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason       TEXT NOT NULL,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_strikes_guild_user ON strikes (guild_id, user_id);
CREATE TABLE IF NOT EXISTS layout_backups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    snapshot   TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_backups_guild ON layout_backups (guild_id, created_at);
"""


class Database:
    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        db_path = Path(self._path)
        if db_path.parent != db_path:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() has not been called")
        return self._conn

    # ---- guild settings (JSON-encoded key/value) ----

    async def get_setting(self, guild_id: int, key: str, default: Any = None) -> Any:
        async with self.conn.execute(
            "SELECT value FROM guild_settings WHERE guild_id = ? AND key = ?",
            (guild_id, key),
        ) as cur:
            row = await cur.fetchone()
        return json.loads(row["value"]) if row else default

    async def set_setting(self, guild_id: int, key: str, value: Any) -> None:
        await self.conn.execute(
            "INSERT INTO guild_settings (guild_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT (guild_id, key) DO UPDATE SET value = excluded.value",
            (guild_id, key, json.dumps(value)),
        )
        await self.conn.commit()

    # ---- strikes ----

    async def add_strike(
        self, guild_id: int, user_id: int, moderator_id: int, reason: str
    ) -> int:
        """Insert a strike; return the member's new total strike count."""
        await self.conn.execute(
            "INSERT INTO strikes (guild_id, user_id, moderator_id, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (guild_id, user_id, moderator_id, reason, time.time()),
        )
        await self.conn.commit()
        return await self.count_strikes(guild_id, user_id)

    async def count_strikes(self, guild_id: int, user_id: int) -> int:
        async with self.conn.execute(
            "SELECT COUNT(*) AS n FROM strikes WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ) as cur:
            row = await cur.fetchone()
        return int(row["n"])

    async def list_strikes(self, guild_id: int, user_id: int) -> list[dict]:
        async with self.conn.execute(
            "SELECT id, moderator_id, reason, created_at FROM strikes "
            "WHERE guild_id = ? AND user_id = ? ORDER BY id DESC",
            (guild_id, user_id),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def clear_strikes(self, guild_id: int, user_id: int) -> int:
        cur = await self.conn.execute(
            "DELETE FROM strikes WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        await self.conn.commit()
        return cur.rowcount or 0

    # ---- layout backups ----

    async def save_layout_backup(self, guild_id: int, snapshot: dict) -> int:
        cur = await self.conn.execute(
            "INSERT INTO layout_backups (guild_id, snapshot, created_at) VALUES (?, ?, ?)",
            (guild_id, json.dumps(snapshot), time.time()),
        )
        await self.conn.commit()
        return int(cur.lastrowid)

    async def latest_backup(self, guild_id: int) -> Optional[dict]:
        async with self.conn.execute(
            "SELECT id, snapshot, created_at FROM layout_backups "
            "WHERE guild_id = ? ORDER BY id DESC LIMIT 1",
            (guild_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "created_at": row["created_at"],
            "snapshot": json.loads(row["snapshot"]),
        }

    async def list_backups(self, guild_id: int, limit: int = 10) -> list[dict]:
        async with self.conn.execute(
            "SELECT id, created_at FROM layout_backups WHERE guild_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (guild_id, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]
