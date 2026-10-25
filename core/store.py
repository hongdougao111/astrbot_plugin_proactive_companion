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

try:  # 正常：作为 core 包的一部分被导入
    from . import affection as affection_mod
except ImportError:  # pragma: no cover - 独立加载时的兜底
    import importlib.util
    import sys as _sys

    _spec = importlib.util.spec_from_file_location(
        "pac_affection", str(Path(__file__).with_name("affection.py"))
    )
    _module = importlib.util.module_from_spec(_spec)
    _sys.modules["pac_affection"] = _module
    if _spec.loader is not None:
        _spec.loader.exec_module(_module)
    affection_mod = _module  # type: ignore[assignment]

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
    atmosphere_level TEXT DEFAULT '',
    atmosphere_data TEXT DEFAULT '',
    atmosphere_updated_at REAL NOT NULL DEFAULT 0,
    mood_updated_at REAL NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL DEFAULT 0,
    note            TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    role        TEXT NOT NULL,
    sender      TEXT DEFAULT '',
    sender_id   TEXT DEFAULT '',
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

CREATE TABLE IF NOT EXISTS user_profiles (
    session_key   TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    nickname      TEXT DEFAULT '',
    affection     REAL NOT NULL DEFAULT 0,
    tags          TEXT DEFAULT '[]',
    note          TEXT DEFAULT '',
    msg_count     INTEGER NOT NULL DEFAULT 0,
    bot_replies   INTEGER NOT NULL DEFAULT 0,
    first_seen    REAL NOT NULL DEFAULT 0,
    last_seen     REAL NOT NULL DEFAULT 0,
    affection_at  REAL NOT NULL DEFAULT 0,
    nickname_manual INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_key, user_id)
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
                "atmosphere_level": "TEXT DEFAULT ''",
                "atmosphere_data": "TEXT DEFAULT ''",
                "atmosphere_updated_at": "REAL NOT NULL DEFAULT 0",
            },
            "messages": {
                "sender_id": "TEXT DEFAULT ''",
            },
            "user_profiles": {
                "nickname_manual": "INTEGER NOT NULL DEFAULT 0",
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
            "atmosphere_level",
            "atmosphere_data",
            "atmosphere_updated_at",
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
        sender_id: str = "",
        ts: float | None = None,
    ) -> None:
        """写入一条消息。sender 是昵称，sender_id 是用户唯一标识。"""
        ts = time.time() if ts is None else ts

        def _do(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO messages (session_key, role, sender, sender_id, content, ts)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (session_key, role, sender, sender_id, content, ts),
            )

        await self._run(_do)

    async def recent_messages(
        self, session_key: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        def _do(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT role, sender, sender_id, content, ts FROM messages"
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
    # 气氛
    # ------------------------------------------------------------------
    async def update_atmosphere(
        self, session_key: str, atmosphere: dict[str, Any]
    ) -> None:
        """保存某个会话的气氛快照。"""
        try:
            payload = json.dumps(atmosphere, ensure_ascii=False)
        except (TypeError, ValueError):
            payload = "{}"

        def _do(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE sessions SET atmosphere_level = ?, atmosphere_data = ?,"
                " atmosphere_updated_at = ? WHERE session_key = ?",
                (
                    str(atmosphere.get("level") or ""),
                    payload,
                    float(atmosphere.get("updated_at") or 0.0),
                    session_key,
                ),
            )

        await self._run(_do)

    async def get_atmosphere(self, session_key: str) -> dict[str, Any] | None:
        """读取某个会话最近一次的气象快照。"""

        def _do(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                "SELECT atmosphere_data FROM sessions WHERE session_key = ?",
                (session_key,),
            ).fetchone()
            if row is None or not row["atmosphere_data"]:
                return None
            try:
                return json.loads(row["atmosphere_data"])
            except (TypeError, ValueError):
                return None

        return await self._run(_do)

    # ------------------------------------------------------------------
    # 人物画像
    # ------------------------------------------------------------------
    async def touch_profile(
        self,
        session_key: str,
        user_id: str,
        nickname: str = "",
        affection_delta: float = 0.0,
        event: str = "",
        positive: bool = False,
        negative: bool = False,
        now: float | None = None,
    ) -> dict[str, Any]:
        """记录一次用户互动，并更新好感度。

        affection_delta 为额外加减分（由情绪分析得出），
        event 用于按事件类型追加基础分。
        """
        now = time.time() if now is None else now

        def _do(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute(
                "SELECT * FROM user_profiles WHERE session_key = ? AND user_id = ?",
                (session_key, user_id),
            ).fetchone()
            if row is None:
                score = 0.0
                first_seen = now
                msg_count = 0
                bot_replies = 0
            else:
                score = float(row["affection"] or 0.0)
                first_seen = float(row["first_seen"] or now)
                msg_count = int(row["msg_count"] or 0)
                bot_replies = int(row["bot_replies"] or 0)

            # 先按无互动时长衰减
            score = affection_mod.decayed(
                score, float(row["affection_at"]) if row else 0.0, now
            )
            score = affection_mod.apply_event(
                score, event, positive=positive, negative=negative
            )
            score = affection_mod.clamp(score + affection_delta)

            if event == "user_message":
                msg_count += 1
            elif event in ("bot_reply", "proactive_reply"):
                bot_replies += 1

            conn.execute(
                "INSERT INTO user_profiles (session_key, user_id, nickname, affection,"
                " tags, note, msg_count, bot_replies, first_seen, last_seen,"
                " affection_at) VALUES (?, ?, ?, ?, '[]', '', ?, ?, ?, ?, ?)"
                " ON CONFLICT(session_key, user_id) DO UPDATE SET"
                " nickname = CASE WHEN user_profiles.nickname_manual = 1"
                " AND user_profiles.nickname <> '' THEN user_profiles.nickname"
                " ELSE excluded.nickname END,"
                " affection = excluded.affection,"
                " msg_count = excluded.msg_count, bot_replies = excluded.bot_replies,"
                " first_seen = CASE WHEN user_profiles.first_seen > 0"
                " AND user_profiles.first_seen < excluded.first_seen"
                " THEN user_profiles.first_seen ELSE excluded.first_seen END,"
                " last_seen = excluded.last_seen, affection_at = excluded.affection_at",
                (
                    session_key,
                    user_id,
                    nickname,
                    score,
                    msg_count,
                    bot_replies,
                    first_seen,
                    now,
                    now,
                ),
            )
            out = conn.execute(
                "SELECT * FROM user_profiles WHERE session_key = ? AND user_id = ?",
                (session_key, user_id),
            ).fetchone()
            return dict(out) if out else {}

        return await self._run(_do)

    async def get_profile(
        self, session_key: str, user_id: str
    ) -> dict[str, Any] | None:
        def _do(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                "SELECT * FROM user_profiles WHERE session_key = ? AND user_id = ?",
                (session_key, user_id),
            ).fetchone()
            return dict(row) if row else None

        return await self._run(_do)

    async def list_profiles(
        self, session_key: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        def _do(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            if session_key:
                rows = conn.execute(
                    "SELECT * FROM user_profiles WHERE session_key = ?"
                    " ORDER BY affection DESC LIMIT ?",
                    (session_key, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM user_profiles ORDER BY affection DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]

        return await self._run(_do)

    async def update_profile_fields_by_user(
        self,
        user_id: str,
        session_key: str = "",
        *,
        note: str | None = None,
        tags: list[str] | None = None,
        affection: float | None = None,
        nickname: str | None = None,
    ) -> list[dict[str, Any]]:
        """按「人」修改画像：同步更新该用户在**所有会话**里的画像。

        同一个人可能同时在群聊和私聊里跟机器人说话，画像按会话各存了一份，
        只改其中一份的话，另一份看起来就像「没生效」。这里统一按 user_id
        写入所有会话的画像；如果这个人还没有任何画像行，就新建一行。

        返回更新后的全部画像行。
        """
        user_id = (user_id or "").strip()
        if not user_id:
            return []

        def _do(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT session_key FROM user_profiles WHERE user_id = ?"
                " ORDER BY last_seen DESC",
                (user_id,),
            ).fetchall()
            keys = [str(r["session_key"]) for r in rows]
            if not keys:
                keys = [session_key or ""]
            updated: list[dict[str, Any]] = []
            for key in keys:
                profile = _update_one(
                    conn,
                    key,
                    user_id,
                    note=note,
                    tags=tags,
                    affection=affection,
                    nickname=nickname,
                )
                if profile:
                    updated.append(profile)
            return updated

        def _update_one(
            conn: sqlite3.Connection,
            key: str,
            uid: str,
            *,
            note: str | None,
            tags: list[str] | None,
            affection: float | None,
            nickname: str | None,
        ) -> dict[str, Any] | None:
            """在单个连接里执行更新，返回更新后的行。"""
            sets: list[str] = []
            values: list[Any] = []
            if note is not None:
                sets.append("note = ?")
                values.append(note)
            if tags is not None:
                sets.append("tags = ?")
                values.append(json.dumps(tags, ensure_ascii=False))
            if affection is not None:
                sets.append("affection = ?")
                values.append(affection_mod.clamp(float(affection)))
                sets.append("affection_at = ?")
                values.append(time.time())
            if nickname is not None:
                sets.append("nickname = ?")
                values.append(nickname)
                # 空昵称表示「恢复跟随平台昵称」
                sets.append(
                    "nickname_manual = 0"
                    if not nickname.strip()
                    else "nickname_manual = 1"
                )
            if sets:
                values.extend([key, uid])
                conn.execute(
                    f"UPDATE user_profiles SET {', '.join(sets)}"
                    " WHERE session_key = ? AND user_id = ?",
                    values,
                )
            row = conn.execute(
                "SELECT * FROM user_profiles WHERE session_key = ? AND user_id = ?",
                (key, uid),
            ).fetchone()
            if row is not None:
                return dict(row)
            # 这个会话还没有画像行 → 补一条（昵称/备注等直接落进去）
            now = time.time()
            conn.execute(
                "INSERT INTO user_profiles (session_key, user_id, nickname, affection,"
                " tags, note, msg_count, bot_replies, first_seen, last_seen,"
                " affection_at, nickname_manual)"
                " VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?)",
                (
                    key,
                    uid,
                    nickname or "",
                    affection_mod.clamp(float(affection or 0.0)),
                    json.dumps(tags or [], ensure_ascii=False),
                    note or "",
                    now,
                    now,
                    now,
                    1 if nickname else 0,
                ),
            )
            row = conn.execute(
                "SELECT * FROM user_profiles WHERE session_key = ? AND user_id = ?",
                (key, uid),
            ).fetchone()
            return dict(row) if row else None

        return await self._run(_do)

    async def recent_messages_by_user(
        self, user_id: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """取某个人最近说过的话（跨会话）。"""

        def _do(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT session_key, role, sender, content, ts FROM messages"
                " WHERE sender_id = ? AND role = 'user'"
                " ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]

        return await self._run(_do)

    async def user_profile_sessions(self, user_id: str) -> list[dict[str, Any]]:
        """某个人在所有会话里的画像行（按最近活跃排序）。"""

        def _do(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT * FROM user_profiles WHERE user_id = ? ORDER BY last_seen DESC",
                (user_id,),
            ).fetchall()
            return [dict(r) for r in rows]

        return await self._run(_do)

    async def update_profile_fields(
        self,
        session_key: str,
        user_id: str,
        *,
        note: str | None = None,
        tags: list[str] | None = None,
        affection: float | None = None,
        nickname: str | None = None,
    ) -> dict[str, Any] | None:
        """手动修改画像（备注 / 标签 / 好感度 / 昵称）。"""

        def _do(conn: sqlite3.Connection) -> dict[str, Any] | None:
            sets: list[str] = []
            values: list[Any] = []
            if note is not None:
                sets.append("note = ?")
                values.append(note)
            if tags is not None:
                sets.append("tags = ?")
                values.append(json.dumps(tags, ensure_ascii=False))
            if affection is not None:
                sets.append("affection = ?")
                values.append(affection_mod.clamp(float(affection)))
                sets.append("affection_at = ?")
                values.append(time.time())
            if nickname is not None:
                sets.append("nickname = ?")
                values.append(nickname)
                # 标记为手动设置，后续互动不再用平台昵称覆盖；
                # 昵称留空则表示「恢复跟随平台昵称」
                sets.append(
                    "nickname_manual = 0"
                    if not nickname.strip()
                    else "nickname_manual = 1"
                )
            if not sets:
                row = conn.execute(
                    "SELECT * FROM user_profiles WHERE session_key = ? AND user_id = ?",
                    (session_key, user_id),
                ).fetchone()
                return dict(row) if row else None
            values.extend([session_key, user_id])
            conn.execute(
                f"UPDATE user_profiles SET {', '.join(sets)}"
                " WHERE session_key = ? AND user_id = ?",
                values,
            )
            row = conn.execute(
                "SELECT * FROM user_profiles WHERE session_key = ? AND user_id = ?",
                (session_key, user_id),
            ).fetchone()
            return dict(row) if row else None

        return await self._run(_do)

    async def backfill_profiles(self) -> int:
        """根据历史聊天记录回填人物画像。

        插件升级前积累的聊天记录也应该体现在画像里（消息数、首末出现时间、
        昵称等）。这里只做「补齐」：已有画像保留手动设置与已累积的好感度，
        缺失的部分用历史记录补上；完全没见过的用户则新建画像。

        返回新建 / 更新的画像条数。
        """

        def _do(conn: sqlite3.Connection) -> int:
            bot_counts = {
                str(row["session_key"]): int(row["c"])
                for row in conn.execute(
                    "SELECT session_key, COUNT(*) AS c FROM messages"
                    " WHERE role = 'bot' GROUP BY session_key"
                ).fetchall()
            }
            rows = conn.execute(
                "SELECT session_key, sender_id, sender, COUNT(*) AS c,"
                " MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM messages"
                " WHERE role = 'user' AND sender_id <> ''"
                " GROUP BY session_key, sender_id"
            ).fetchall()
            changed = 0
            for row in rows:
                session_key = str(row["session_key"])
                user_id = str(row["sender_id"])
                count = int(row["c"] or 0)
                first_ts = float(row["first_ts"] or 0.0)
                last_ts = float(row["last_ts"] or 0.0)
                sender = str(row["sender"] or "")
                bot_count = bot_counts.get(session_key, 0)
                existing = conn.execute(
                    "SELECT * FROM user_profiles WHERE session_key = ? AND user_id = ?",
                    (session_key, user_id),
                ).fetchone()
                if existing is None:
                    score = affection_mod.clamp(
                        count * affection_mod.DELTA_USER_MESSAGE
                    )
                    conn.execute(
                        "INSERT INTO user_profiles (session_key, user_id, nickname,"
                        " affection, tags, note, msg_count, bot_replies, first_seen,"
                        " last_seen, affection_at, nickname_manual)"
                        " VALUES (?, ?, ?, ?, '[]', '', ?, ?, ?, ?, ?, 0)",
                        (
                            session_key,
                            user_id,
                            sender,
                            score,
                            count,
                            bot_count,
                            first_ts,
                            last_ts,
                            last_ts,
                        ),
                    )
                    changed += 1
                    continue
                # 已有画像：只补齐缺失的部分，不覆盖手动设置
                nickname = str(existing["nickname"] or "")
                manual = int(existing["nickname_manual"] or 0)
                if not nickname or (not manual and sender and sender != nickname):
                    nickname = sender
                msg_count = max(int(existing["msg_count"] or 0), count)
                bot_replies = max(int(existing["bot_replies"] or 0), bot_count)
                first_seen = float(existing["first_seen"] or 0.0)
                if first_seen <= 0 or (first_ts > 0 and first_ts < first_seen):
                    first_seen = first_ts
                last_seen = max(float(existing["last_seen"] or 0.0), last_ts)
                score = float(existing["affection"] or 0.0)
                if score <= 0 and count > 0:
                    score = affection_mod.clamp(
                        count * affection_mod.DELTA_USER_MESSAGE
                    )
                conn.execute(
                    "UPDATE user_profiles SET nickname = ?, affection = ?,"
                    " msg_count = ?, bot_replies = ?, first_seen = ?, last_seen = ?"
                    " WHERE session_key = ? AND user_id = ?",
                    (
                        nickname,
                        score,
                        msg_count,
                        bot_replies,
                        first_seen,
                        last_seen,
                        session_key,
                        user_id,
                    ),
                )
                changed += 1
            return changed

        return await self._run(_do)

    # ------------------------------------------------------------------
    # 一键清除记忆
    # ------------------------------------------------------------------
    async def clear_session_memory(
        self, session_key: str, base_arousal: float = 0.5, reset_mood: bool = True
    ) -> dict[str, int]:
        """清除某个会话的记忆。

        删除该会话的聊天记录与人物画像，并把情绪、气氛快照重置。
        统计数据（主动消息次数、每日额度）保留，避免被当成绕过限流的手段。
        """

        def _do(conn: sqlite3.Connection) -> dict[str, int]:
            messages = conn.execute(
                "DELETE FROM messages WHERE session_key = ?", (session_key,)
            ).rowcount
            profiles = conn.execute(
                "DELETE FROM user_profiles WHERE session_key = ?", (session_key,)
            ).rowcount
            if reset_mood:
                conn.execute(
                    "UPDATE sessions SET valence = 0.0, arousal = ?, mood_updated_at = ?,"
                    " last_msg_at = 0.0, atmosphere_level = '', atmosphere_data = '',"
                    " atmosphere_updated_at = 0.0 WHERE session_key = ?",
                    (base_arousal, time.time(), session_key),
                )
            return {"messages": messages, "profiles": profiles}

        return await self._run(_do)

    async def clear_user_memory(
        self, user_id: str, keep_messages: bool = False
    ) -> dict[str, int]:
        """清除某个人的记忆。

        删除该用户在所有会话里的画像（昵称、标签、备注、好感度）。
        keep_messages 为 False 时，一并删除他说过的话；机器人自己的发言保留。
        """
        user_id = (user_id or "").strip()

        def _do(conn: sqlite3.Connection) -> dict[str, int]:
            profiles = conn.execute(
                "DELETE FROM user_profiles WHERE user_id = ?", (user_id,)
            ).rowcount
            messages = 0
            if not keep_messages:
                messages = conn.execute(
                    "DELETE FROM messages WHERE sender_id = ?", (user_id,)
                ).rowcount
            return {"messages": messages, "profiles": profiles}

        return await self._run(_do)

    async def clear_all_memory(
        self,
        keep_messages: bool = False,
        base_arousal: float = 0.5,
        reset_mood: bool = True,
    ) -> dict[str, int]:
        """清除全部记忆（所有会话）。"""

        def _do(conn: sqlite3.Connection) -> dict[str, int]:
            profiles = conn.execute("DELETE FROM user_profiles").rowcount
            messages = 0
            if not keep_messages:
                messages = conn.execute("DELETE FROM messages").rowcount
            if reset_mood:
                conn.execute(
                    "UPDATE sessions SET valence = 0.0, arousal = ?, mood_updated_at = ?,"
                    " last_msg_at = 0.0, atmosphere_level = '', atmosphere_data = '',"
                    " atmosphere_updated_at = 0.0",
                    (base_arousal, time.time()),
                )
            return {"messages": messages, "profiles": profiles}

        return await self._run(_do)

    async def latest_sender_names(
        self, session_key: str
    ) -> tuple[dict[str, str], str, str]:
        """返回 (每个用户 ID 的最新昵称, 最近发言者 ID, 最近发言者昵称)。

        用户改了昵称之后，历史消息里存的还是旧名字；这里按「最新一次出现的
        名字」给出映射，方便把上下文里的旧名字统一替换成新名字。
        """
        session_key = (session_key or "").strip()

        def _do(conn: sqlite3.Connection) -> tuple[dict[str, str], str, str]:
            rows = conn.execute(
                "SELECT sender_id, sender FROM messages"
                " WHERE session_key = ? AND role = 'user' AND sender_id <> ''"
                " AND id IN (SELECT MAX(id) FROM messages WHERE session_key = ?"
                " AND role = 'user' AND sender_id <> '' GROUP BY sender_id)",
                (session_key, session_key),
            ).fetchall()
            names = {str(r["sender_id"]): str(r["sender"] or "") for r in rows}
            peer = conn.execute(
                "SELECT sender_id, sender FROM messages WHERE session_key = ?"
                " AND role = 'user' AND sender_id <> '' ORDER BY id DESC LIMIT 1",
                (session_key,),
            ).fetchone()
            peer_id = str(peer["sender_id"]) if peer else ""
            peer_name = str(peer["sender"] or "") if peer else ""
            return names, peer_id, peer_name

        return await self._run(_do)

    async def recent_user_messages(
        self, session_key: str, user_id: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """取某个用户最近说过的话（用于画像展示聊天足迹）。"""

        def _do(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT role, sender, content, ts FROM messages"
                " WHERE session_key = ? AND sender_id = ?"
                " ORDER BY id DESC LIMIT ?",
                (session_key, user_id, limit),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]

        return await self._run(_do)

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
