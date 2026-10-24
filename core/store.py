"""持久化存储层。

使用标准库 sqlite3，数据文件位于 AstrBot 的 data/plugin_data/<插件名>/ 目录下，
避免插件更新或重装时数据被覆盖。所有耗时操作都通过 asyncio.to_thread 放到线程池，
不阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_key      TEXT PRIMARY KEY,
    umo             TEXT NOT NULL,
    platform        TEXT DEFAULT '',
    chat_type       TEXT DEFAULT '',
    title           TEXT DEFAULT '',
    enabled         INTEGER NOT NULL DEFAULT 1,
    last_msg_at     REAL NOT NULL DEFAULT 0,
    last_proactive_at REAL NOT NULL DEFAULT 0,
    messages_sent   INTEGER NOT NULL DEFAULT 0,
    proactive_replies INTEGER NOT NULL DEFAULT 0,
    daily_count     INTEGER NOT NULL DEFAULT 0,
    daily_date      TEXT DEFAULT '',
    valence         REAL NOT NULL DEFAULT 0,
    arousal         REAL NOT NULL DEFAULT 0.3,
    mood_updated_at REAL NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL DEFAULT 0,
    note            TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    role        TEXT NOT NULL,
    sender      TEXT DEFAULT '',
    content     TEXT DEFAULT '',
    ts          REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session
    ON messages(session_key, id DESC);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT DEFAULT '',
    kind        TEXT NOT NULL,
    payload     TEXT DEFAULT '',
    ts          REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(id DESC);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Store:
    """基于 sqlite3 的轻量持久化存储。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._init_schema()

    # ------------------------------------------------------------------
    # 基础设施
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _run_sync(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        conn = self._connect()
        try:
            with conn:
                return fn(conn)
        finally:
            conn.close()

    async def _run(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        async with self._lock:
            return await asyncio.to_thread(self._run_sync, fn)

    def _init_schema(self) -> None:
        def _do(conn: sqlite3.Connection) -> None:
            conn.executescript(SCHEMA)
            self._migrate(conn)

        self._run_sync(_do)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """为旧版本数据库补齐新增列（插件升级后不丢数据）。"""
        additions = {
            "sessions": {
                "proactive_replies": "INTEGER NOT NULL DEFAULT 0",
            },
        }
        for table, columns in additions.items():
            try:
                existing = {
                    row["name"]
                    for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
            except sqlite3.Error:
                continue
            if not existing:
                continue
            for column, ddl in columns.items():
                if column not in existing:
                    try:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                    except sqlite3.Error:
                        pass

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------
    async def ensure_session(
        self,
        session_key: str,
        umo: str,
        platform: str = "",
        chat_type: str = "",
        title: str = "",
    ) -> dict[str, Any]:
        """确保会话存在并返回会话记录。"""

        def _do(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_key = ?", (session_key,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO sessions (session_key, umo, platform, chat_type, title,"
                    " created_at, mood_updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_key,
                        umo,
                        platform,
                        chat_type,
                        title,
                        time.time(),
                        time.time(),
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM sessions WHERE session_key = ?", (session_key,)
                ).fetchone()
            else:
                # 补充可能缺失的字段
                if not row["umo"]:
                    conn.execute(
                        "UPDATE sessions SET umo = ?, platform = ?, chat_type = ?,"
                        " title = ? WHERE session_key = ?",
                        (umo, platform, chat_type, title, session_key),
                    )
                    row = conn.execute(
                        "SELECT * FROM sessions WHERE session_key = ?", (session_key,)
                    ).fetchone()
            return dict(row) if row else None

        return await self._run(_do)

    async def get_session(self, session_key: str) -> dict[str, Any] | None:
        def _do(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_key = ?", (session_key,)
            ).fetchone()
            return dict(row) if row else None

        return await self._run(_do)

    async def list_sessions(self, limit: int = 500) -> list[dict[str, Any]]:
        def _do(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY last_msg_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

        return await self._run(_do)

    async def update_session(
        self, session_key: str, **fields: Any
    ) -> dict[str, Any] | None:
        if not fields:
            return await self.get_session(session_key)
        allowed = {
            "umo",
            "platform",
            "chat_type",
            "title",
            "enabled",
            "last_msg_at",
            "last_proactive_at",
            "messages_sent",
            "proactive_replies",
            "daily_count",
            "daily_date",
            "valence",
            "arousal",
            "mood_updated_at",
            "note",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return await self.get_session(session_key)

        def _do(conn: sqlite3.Connection) -> dict[str, Any] | None:
            sets = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(
                f"UPDATE sessions SET {sets} WHERE session_key = ?",
                (*updates.values(), session_key),
            )
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_key = ?", (session_key,)
            ).fetchone()
            return dict(row) if row else None

        return await self._run(_do)

    async def delete_session(self, session_key: str) -> bool:
        def _do(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                "DELETE FROM sessions WHERE session_key = ?", (session_key,)
            )
            conn.execute("DELETE FROM messages WHERE session_key = ?", (session_key,))
            return cur.rowcount > 0

        return await self._run(_do)

    # ------------------------------------------------------------------
    # 消息
    # ------------------------------------------------------------------
    async def add_message(
        self,
        session_key: str,
        role: str,
        content: str,
        sender: str = "",
        ts: float | None = None,
    ) -> None:
        ts = time.time() if ts is None else ts

        def _do(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO messages (session_key, role, sender, content, ts)"
                " VALUES (?, ?, ?, ?, ?)",
                (session_key, role, sender, content, ts),
            )

        await self._run(_do)

    async def recent_messages(
        self, session_key: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        def _do(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT role, sender, content, ts FROM messages"
                " WHERE session_key = ? ORDER BY id DESC LIMIT ?",
                (session_key, limit),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]

        return await self._run(_do)

    async def prune_messages(self, keep_per_session: int = 200) -> None:
        """仅保留每个会话最近的若干条消息，避免数据库无限增长。"""

        def _do(conn: sqlite3.Connection) -> None:
            rows = conn.execute("SELECT session_key FROM sessions").fetchall()
            for row in rows:
                conn.execute(
                    "DELETE FROM messages WHERE session_key = ? AND id NOT IN ("
                    "SELECT id FROM messages WHERE session_key = ?"
                    " ORDER BY id DESC LIMIT ?)",
                    (row["session_key"], row["session_key"], keep_per_session),
                )

        await self._run(_do)

    # ------------------------------------------------------------------
    # 事件日志
    # ------------------------------------------------------------------
    async def add_event(
        self,
        kind: str,
        session_key: str = "",
        payload: dict[str, Any] | str | None = None,
    ) -> None:
        if payload is None:
            text = ""
        elif isinstance(payload, str):
            text = payload
        else:
            try:
                text = json.dumps(payload, ensure_ascii=False)
            except (TypeError, ValueError):
                text = str(payload)

        def _do(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO events (session_key, kind, payload, ts) VALUES (?, ?, ?, ?)",
                (session_key, kind, text[:4000], time.time()),
            )

        await self._run(_do)

    async def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        def _do(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT id, session_key, kind, payload, ts FROM events"
                " ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

        return await self._run(_do)

    async def prune_events(self, keep: int = 1000) -> None:
        def _do(conn: sqlite3.Connection) -> None:
            conn.execute(
                "DELETE FROM events WHERE id NOT IN"
                " (SELECT id FROM events ORDER BY id DESC LIMIT ?)",
                (keep,),
            )

        await self._run(_do)

    # ------------------------------------------------------------------
    # KV
    # ------------------------------------------------------------------
    async def kv_get(self, key: str, default: Any = None) -> Any:
        def _do(conn: sqlite3.Connection) -> Any:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
            if row is None:
                return default
            try:
                return json.loads(row["value"])
            except (TypeError, ValueError):
                return row["value"]

        return await self._run(_do)

    async def kv_set(self, key: str, value: Any) -> None:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = json.dumps(str(value))

        def _do(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, text),
            )

        await self._run(_do)
