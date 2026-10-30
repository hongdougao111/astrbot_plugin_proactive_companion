"""AstrBot 主动陪伴插件。

让 Bot 能在私聊与群聊中**主动发起消息**，并提供：

- **主动消息**：定时巡检会话，在满足条件时主动开口。
- **主动回复**：在机器人本来不会回复的消息上，按概率 + 白名单主动接话。
- **上下文感知**：记录每个会话的近期对话，并结合 AstrBot 自身的会话历史生成话题。
- **持久化数据**：会话、消息、情绪、事件日志全部落盘到 data/plugin_data/ 下。
- **动态情绪**：valence/arousal 双维度情绪，随互动漂移并随时间衰减。
- **免打扰时段**：支持跨午夜的静默区间，静默期间不主动打扰。
- **TTS 集成**：可选用 AstrBot 的 TTS Provider 把主动消息转成语音。
- **独立 WebUI**：插件自带可视化面板，可管理会话、情绪、配置与日志。

指令：
- `/陪伴 开启` `/陪伴 关闭` `/陪伴 状态` `/陪伴 说说` `/陪伴 清空`
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import random
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.event.filter import CustomFilter
from astrbot.api.message_components import Record
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import error_response, json_response, request

try:
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover - 极端情况下退回不注入
    TextPart = None  # type: ignore[assignment]

try:
    # 正常情况：AstrBot 以 `data.plugins.<插件名>.main` 的形式加载插件，相对导入可用。
    from .core import affection, atmosphere, dnd, emotion, generator
    from .core.store import Store
except ImportError:  # pragma: no cover - 兜底：插件被以独立脚本方式加载时
    import importlib
    import sys as _sys
    from pathlib import Path as _Path

    _plugin_dir = str(_Path(__file__).resolve().parent)
    if _plugin_dir not in _sys.path:
        _sys.path.insert(0, _plugin_dir)
    affection = importlib.import_module("core.affection")
    atmosphere = importlib.import_module("core.atmosphere")
    dnd = importlib.import_module("core.dnd")
    emotion = importlib.import_module("core.emotion")
    generator = importlib.import_module("core.generator")
    Store = importlib.import_module("core.store").Store

PLUGIN_NAME = "astrbot_plugin_proactive_companion"

SAFE_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "check_interval_seconds": 60,
    "quiet_hours": [],
    "min_idle_minutes": 45,
    "cooldown_minutes": 180,
    "max_daily_messages": 6,
    "max_proactive_per_tick": 3,
    "idle_jitter_ratio": 0.2,
    "group_enabled": True,
    "private_enabled": True,
    "mention_only_in_group": False,
    # 私聊专属阈值：默认比全局更主动，设为 0 / 留空则回退到全局配置
    "private_min_idle_minutes": 15,
    "private_cooldown_minutes": 60,
    "private_max_daily_messages": 10,
    "private_quiet_hours": [],
    "private_skip_atmosphere": True,
    "private_priority": True,
    "private_proactive_whitelist": [],
    # 名单类配置统一约定：黑名单优先于白名单；留空或填 `*` 表示不限制。
    "private_proactive_blacklist": [],
    "group_proactive_whitelist": [],
    "group_proactive_blacklist": [],
    "proactive_reply_blacklist": [],
    "mood_enabled": True,
    "mood_decay_per_hour": 0.15,
    "mood_affect_frequency": True,
    "affection_enabled": True,
    "tts_enabled": False,
    "tts_provider_id": "",
    "tts_send_text": True,
    "tts_max_chars": 120,
    "system_prompt": generator.DEFAULT_SYSTEM_PROMPT,
    "max_context_messages": 12,
    "keep_messages_per_session": 200,
    "keep_messages_days": 30,
    "keep_events": 1000,
    "proactive_reply_enabled": True,
    "proactive_reply_probability": 0.2,
    "proactive_reply_whitelist": [],
    "proactive_reply_cooldown_minutes": 10,
    "proactive_reply_min_length": 2,
    "capture_context_enabled": True,
    "proactive_reply_context_messages": 12,
    "capture_context_max_sessions": 200,
    # 上下文注入的护栏：防止注入的内容触发 LLM 网关的输入安全拦截
    "context_injection_enabled": True,
    "context_max_chars": 1200,
    "context_max_message_chars": 200,
    "context_blocked_keywords": [],
    # 去掉 LLM 请求历史里重复的消息：平台重复投递时，模型会以为
    # 「对方连着说了好几遍」，甚至在回复里直接说出来。
    "dedupe_llm_context_enabled": True,
    "atmosphere_enabled": True,
    "atmosphere_window_minutes": 30,
    "atmosphere_refresh_minutes": 3,
    "atmosphere_cold_rate": 0.12,
    "atmosphere_hot_rate": 0.8,
    "atmosphere_conflict_ratio": 0.15,
    "atmosphere_boost": 1.5,
}

# 插件实例引用。过滤器在模块导入时即被实例化，拿不到插件实例，
# 因此这里用模块级引用在运行时取回当前插件。
_PLUGIN_REF: ProactiveCompanionPlugin | None = None

# 事件上的临时标记：这条消息已经被插件记录过，避免重复入库
_CAPTURED_FLAG = "_pac_message_captured"
# 主动回复事件上的标记：本次 LLM 请求需要注入插件记录的上下文
_PROACTIVE_SESSION_KEY = "_pac_proactive_session"
# 标记：这条消息触发的是主动回复，好感度与回应次数已经计过
_PROACTIVE_REPLY_FLAG = "_pac_proactive_reply"


class ProactiveReplyFilter(CustomFilter):
    """决定一条消息是否触发「主动回复」。

    过滤器对每一条消息都会被求值，因此这里顺带做**被动上下文采集**：
    把真实对话记录下来，主动回复时才有上下文可用。

    只有当消息本身**不会**唤醒机器人时（即群里没被 @、私聊需要唤醒词等情况）
    才可能触发，避免与正常回复重复。
    """

    def __init__(self, raise_error: bool = True, **kwargs: Any) -> None:
        super().__init__(raise_error)

    def filter(self, event: AstrMessageEvent, cfg: Any) -> bool:
        plugin = _PLUGIN_REF
        if plugin is None:
            return False
        try:
            plugin.capture_message_background(event)
            return plugin.should_proactive_reply(event, cfg)
        except Exception as exc:  # pragma: no cover - 过滤器绝不能抛异常
            logger.warning(f"[主动陪伴] 主动回复判定失败: {exc}")
            return False


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _safe_json_list(raw: Any) -> list[Any]:
    """安全解析标签数组，坏数据不炸。"""
    if isinstance(raw, list):
        return list(raw)
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _profile_summary(item: dict[str, Any]) -> str:
    """生成一句话人物速写。"""
    name = str(item.get("nickname") or item.get("user_id") or "这个人")
    level = str(item.get("level") or "")
    msg_count = int(item.get("msg_count") or 0)
    bot_replies = int(item.get("bot_replies") or 0)
    session_count = int(item.get("session_count") or 1)
    parts = [f"{name}·{level}" if level else name]
    if msg_count:
        parts.append(f"聊过 {msg_count} 条，机器人接了 {bot_replies} 次")
    if session_count > 1:
        parts.append(f"在 {session_count} 个会话里活跃")
    tags = item.get("tags") or []
    if tags:
        parts.append("标签：" + "、".join(str(t) for t in tags))
    recent = item.get("recent_messages") or []
    if recent:
        last = str(recent[-1].get("content") or "").replace("\n", " ").strip()
        if len(last) > 20:
            last = last[:20] + "…"
        parts.append(f"最近提到「{last}」")
    note = str(item.get("note") or "").replace("\n", " ").strip()
    if note:
        if len(note) > 30:
            note = note[:30] + "…"
        parts.append(f"备注：{note}")
    return " ｜ ".join(parts)


class ProactiveCompanionPlugin(Star):
    """主动陪伴插件主体。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config if config is not None else AstrBotConfig()
        self._normalize_config()
        self.data_dir: Path = StarTools.get_data_dir(PLUGIN_NAME)
        self.store = Store(self.data_dir / "proactive.db")
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._last_tick_at: float = 0.0
        self._last_tick_error: str = ""
        # 主动回复：记录每个会话上次触发的时间，用于冷却
        self._proactive_reply_times: dict[str, float] = {}
        # 已经触发过主动回复的消息 ID -> 时间戳：防止同一条消息被重复投递时连发多条
        self._reply_triggered_message_ids: dict[str, float] = {}
        # 每个会话里「用户 ID -> 最新昵称」的同步缓存，供过滤器里做白名单匹配
        # （过滤器是同步的，不能 await 数据库）
        self._latest_names: dict[str, dict[str, str]] = {}
        # 被动采集：已经被记录过对话的会话
        self._tracked_sessions: set[str] = set()
        # 最近一次主动回复判定结果，便于在面板上排查为什么没触发
        self._last_reply_decision: str = "尚未判定"
        # 各判定原因的跳过次数（面板上展示）
        self._reply_skip_reasons: dict[str, int] = {}
        # 气氛缓存：session_key -> Atmosphere（供同步过滤器快速读取）
        self._atmosphere_cache: dict[str, Any] = {}
        self._atmosphere_refreshed_at: dict[str, float] = {}
        # 好感度缓存：session_key|user_id -> 分数（供同步过滤器快速读取）
        self._affection_cache: dict[str, float] = {}
        global _PLUGIN_REF
        _PLUGIN_REF = self
        self._register_web_apis()

    @staticmethod
    def _plugin_version() -> str:
        """读取插件版本号（诊断信息里用）。"""
        path = Path(__file__).resolve().parent / "metadata.yaml"
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("version:"):
                    return line.split(":", 1)[1].strip().strip("'\"")
        except Exception:  # pragma: no cover
            pass
        return "未知"

    def _schema(self) -> dict[str, Any]:
        """读取 _conf_schema.json，用于按类型规整配置值。"""
        path = Path(__file__).resolve().parent / "_conf_schema.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:  # pragma: no cover
            return {}

    def _coerce_value(self, meta: Any, value: Any) -> Any:
        """把值规整成 schema 声明的类型，避免框架校验报「错误的类型」。"""
        if not isinstance(meta, dict):
            return value
        kind = meta.get("type")
        if kind == "list" and isinstance(value, str):
            parts = value.replace("，", ",").replace("\n", ",").split(",")
            return [p.strip() for p in parts if p.strip()]
        if kind == "float" and isinstance(value, int) and not isinstance(value, bool):
            return float(value)
        if kind == "int" and isinstance(value, str):
            try:
                return int(value.strip())
            except (TypeError, ValueError):
                return value
        if kind == "bool" and isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return value

    def _normalize_config(self) -> None:
        """自愈：把历史遗留的类型不符的配置（例如字符串形式的 list）修正并落盘。

        框架对插件配置做严格类型校验，一旦某个 list 字段被写成字符串，
        用户在 WebUI 保存配置时就会报「错误的类型」。这里在插件加载时先修好。
        """
        schema = self._schema()
        changed = False
        for key, meta in schema.items():
            if not isinstance(meta, dict) or "type" not in meta:
                continue
            if key not in self.config:
                continue
            old = self.config.get(key)
            new = self._coerce_value(meta, old)
            # 注意 1 == 1.0，所以类型不同也要当作需要修正
            if new != old or type(new) is not type(old):
                self.config[key] = new
                changed = True
        if changed:
            try:
                self.config.save_config()
                logger.info("[主动陪伴] 已修正配置项类型（list 字段曾被写成字符串）。")
            except Exception as exc:  # pragma: no cover
                logger.warning(f"[主动陪伴] 修正配置类型失败: {exc}")

    # ------------------------------------------------------------------
    # 配置辅助
    # ------------------------------------------------------------------
    def _cfg(self, key: str, default: Any = None) -> Any:
        value = None
        try:
            value = self.config.get(key)
        except Exception:  # pragma: no cover - 配置异常时退回默认值
            value = None
        if value is None:
            value = SAFE_DEFAULTS.get(key, default)
        return value

    def _cfg_bool(self, key: str, default: bool = False) -> bool:
        value = self._cfg(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        try:
            return bool(int(value))
        except (TypeError, ValueError):
            return default

    def _cfg_int(self, key: str, default: int = 0) -> int:
        value = self._cfg(key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _cfg_float(self, key: str, default: float = 0.0) -> float:
        value = self._cfg(key, default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _cfg_str(self, key: str, default: str = "") -> str:
        value = self._cfg(key, default)
        if value is None:
            return default
        return str(value)

    def _cfg_list(self, key: str, default: list | None = None) -> list:
        value = self._cfg(key, default if default is not None else [])
        if isinstance(value, list):
            return value
        if isinstance(value, str) and value.strip():
            return [part.strip() for part in value.split(",") if part.strip()]
        return list(default or [])

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        """AstrBot 启动完成后开启调度循环。"""
        self._stopping = False
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_loop())
            logger.info("[主动陪伴] 调度器已启动。")
        await self.store.add_event("plugin_loaded", payload={"time": _now_str()})
        # 用历史聊天记录补全人物画像（升级前的聊天也算数）
        try:
            count = await self.store.backfill_profiles()
            if count:
                logger.info(f"[主动陪伴] 已根据历史记录补全 {count} 份人物画像。")
        except Exception as exc:  # pragma: no cover
            logger.warning(f"[主动陪伴] 补全人物画像失败: {exc}")

    async def terminate(self) -> None:
        """插件卸载时停止调度循环。"""
        self._stopping = True
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014
                pass
        logger.info("[主动陪伴] 调度器已停止。")

    async def _run_loop(self) -> None:
        while not self._stopping:
            interval = max(15, self._cfg_int("check_interval_seconds", 60))
            try:
                await asyncio.sleep(interval)
                await self._refresh_atmospheres()
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - 兜底保护
                self._last_tick_error = str(exc)
                logger.error(f"[主动陪伴] 调度循环异常: {exc}")

    # ------------------------------------------------------------------
    # 消息监听（上下文感知 + 情绪输入）
    # ------------------------------------------------------------------
    @filter.custom_filter(ProactiveReplyFilter)
    async def proactive_reply(self, event: AstrMessageEvent) -> None:
        """主动回复：命中概率与白名单后，让机器人用平时的人格自然接话。

        这里刻意不产出结果，而是把 `is_at_or_wake_command` 置为 True，
        让 AstrBot 走正常的 LLM 链路回复——这样主动接话的语气、人设、
        上下文都和被 @ 时完全一致。同时打上标记，
        由 `on_llm_request` 钩子把插件记录的对话上下文注入这次请求。
        """
        try:
            event.is_at_or_wake_command = True
            event.set_extra(_PROACTIVE_SESSION_KEY, event.unified_msg_origin)
            await self._on_proactive_reply(event)
        except Exception as exc:  # pragma: no cover - 绝不影响正常对话
            logger.warning(f"[主动陪伴] 主动回复处理失败: {exc}")

    @filter.on_llm_request()
    async def inject_proactive_context(self, event: AstrMessageEvent, req: Any) -> None:
        """把插件记录的对话上下文注入主动回复的 LLM 请求。

        AstrBot 自身的会话历史只包含「机器人参与过」的对话，群里其他人聊天
        并不在里面，所以主动回复必须由插件把上下文补上。
        注入的内容标记为临时内容，不会写入会话历史。

        顺带在这里对 **AstrBot 自己的会话历史做去重**：平台偶尔会把同一条
        消息重复投递给 AstrBot，历史里就会留下好几条一模一样的记录，
        模型看到后会以为「对方连着说了三遍」，甚至直接在回复里说出来。
        """
        # 先去掉请求历史里的重复消息（对所有 LLM 请求生效）
        try:
            removed = self._dedupe_request_contexts(req)
            if removed:
                logger.info(f"[主动陪伴] 已从会话历史中去重 {removed} 条重复消息")
        except Exception as exc:  # pragma: no cover
            logger.warning(f"[主动陪伴] 清理会话历史重复消息失败: {exc}")

        umo = event.get_extra(_PROACTIVE_SESSION_KEY)
        if not umo or TextPart is None:
            return
        if not self._cfg_bool("context_injection_enabled", True):
            return
        try:
            # 该会话是否因为「LLM 输入被安全拦截」而自动熔断了上下文注入
            if await self._context_inject_disabled(umo):
                return
        except Exception:  # pragma: no cover
            pass
        try:
            limit = max(1, self._cfg_int("proactive_reply_context_messages", 12))
            messages = await self.store.recent_messages(umo, limit)
            hint = self._atmosphere_hint(umo)
            try:
                rel = self._relationship_hint(umo, str(event.get_sender_id() or ""))
            except Exception:
                rel = ""
            if rel:
                hint = f"{hint}\n{rel}".strip()
            if not messages and not hint:
                return
            names, _, peer_name = await self._nickname_context(umo)
            text = self._format_context_block(messages, hint, names, peer_name)
            if not text:
                return
            req.extra_user_content_parts.append(TextPart(text=text).mark_as_temp())
        except Exception as exc:  # pragma: no cover
            logger.warning(f"[主动陪伴] 注入上下文失败: {exc}")

    async def _nickname_context(
        self, session_key: str
    ) -> tuple[dict[str, str], str, str]:
        """返回 (用户 ID -> 最新昵称, 主对话对象 ID, 主对话对象昵称)。

        昵称优先级：**手动设置的昵称** > 消息记录里该用户最新出现的名字。
        这样用户改了昵称之后，历史消息里的旧名字也会被替换成新名字，
        模型不会把新旧两个名字当成两个人，也不会继续用旧称呼。
        """
        names: dict[str, str] = {}
        peer_id = ""
        peer_name = ""
        try:
            names, peer_id, peer_name = await self.store.latest_sender_names(
                session_key
            )
        except Exception:  # pragma: no cover
            pass
        try:
            profiles = await self.store.list_profiles(session_key)
        except Exception:  # pragma: no cover
            profiles = []
        for row in profiles:
            uid = str(row.get("user_id") or "")
            nick = str(row.get("nickname") or "").strip()
            if uid and nick and int(row.get("nickname_manual") or 0):
                names[uid] = nick
        if peer_id and names.get(peer_id):
            peer_name = names[peer_id]
        return names, peer_id, peer_name

    async def _context_inject_disabled(self, session_key: str) -> bool:
        """该会话是否已被熔断（不再注入上下文）。"""
        session = await self.store.get_session(session_key)
        if not session:
            return False
        note = self._load_note(session)
        return bool(note.get("context_inject_off"))

    async def _set_context_inject(self, session_key: str, disabled: bool) -> None:
        """设置 / 取消该会话的上下文注入熔断。"""
        session = await self.store.get_session(session_key)
        if not session:
            return
        note = self._load_note(session)
        if disabled:
            note["context_inject_off"] = 1
            note["context_inject_off_at"] = time.time()
        else:
            note.pop("context_inject_off", None)
            note.pop("context_inject_off_at", None)
        await self.store.update_session(
            session_key, note=json.dumps(note, ensure_ascii=False)
        )

    @staticmethod
    def _looks_like_llm_rejection(text: str) -> bool:
        """判断一段回复是不是「LLM 输入被安全拦截」类的失败提示。"""
        lowered = text.lower()
        markers = (
            "all chat models failed",
            "input new_sensitive",
            "sensitive",
            "content_filter",
            "content filter",
            "input_filter",
            "input filter",
        )
        return any(marker in lowered for marker in markers)

    def _sanitize_messages(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """对「要交给模型的消息」做裁剪与屏蔽词过滤。

        为什么需要：插件会把群聊里的对话补进 LLM 请求，而 LLM 网关有输入安全
        审核。一旦某条历史消息被判为敏感，之后**每一次**带上它的请求都会被拦，
        表现为群里每次回复都报「All chat models failed」。这里做两道保险：

        - 命中屏蔽词的消息整条丢弃（关键词由用户配置，默认空 = 不拦任何词）
        - 单条超过长度上限的截断，避免长文把敏感段落带进去
        """
        keywords = [
            str(item).strip().lower()
            for item in self._cfg_list("context_blocked_keywords", [])
            if str(item).strip()
        ]
        # 下限只保证不为空，允许用户把上限调到很小做更严格的裁剪
        max_len = max(1, self._cfg_int("context_max_message_chars", 200))
        result: list[dict[str, Any]] = []
        for msg in messages:
            content = str(msg.get("content") or "")
            if keywords and any(key in content.lower() for key in keywords):
                continue
            if len(content) > max_len:
                content = content[:max_len] + "…"
            item = dict(msg)
            item["content"] = content
            result.append(item)
        return result

    def _format_context_block(
        self,
        messages: list[dict[str, Any]],
        atmosphere_hint: str = "",
        names: dict[str, str] | None = None,
        peer_name: str = "",
    ) -> str:
        """把记录的消息格式化成给 LLM 的上下文块。

        同名用户会被标注用户 ID 片段，避免模型把不同的人当成同一个人；
        改过昵称的用户会统一使用最新昵称，避免新旧两个名字混在一起。
        """
        # 统计「昵称 -> 用户 ID」的映射，判断哪些昵称存在重名
        messages = self._sanitize_messages(messages)
        name_to_ids: dict[str, set[str]] = {}
        for msg in messages:
            name = str(msg.get("sender") or "").strip()
            sid = str(msg.get("sender_id") or "").strip()
            if names and sid and names.get(sid):
                name = names[sid]
            if name and sid:
                name_to_ids.setdefault(name, set()).add(sid)

        def _label(msg: dict[str, Any]) -> str:
            sid = str(msg.get("sender_id") or "").strip()
            name = str(msg.get("sender") or "").strip()
            if names and sid and names.get(sid):
                name = names[sid]
            if not name:
                return sid or "对方"
            if sid and len(name_to_ids.get(name, set())) > 1:
                # 有重名：补上用户 ID 末四位以便区分
                return f"{name}#{sid[-4:]}"
            return name

        lines: list[str] = []
        seen_lines: set[str] = set()
        for msg in messages:
            content = str(msg.get("content") or "").strip()
            if not content:
                continue
            role = str(msg.get("role") or "")
            if role == "user":
                line = f"{_label(msg)}: {content}"
            elif role == "bot":
                line = f"我: {content}"
            elif role == "proactive":
                line = f"我(之前主动搭话): {content}"
            else:
                line = f"{_label(msg)}: {content}"
            # 同一条消息只保留一份：平台重复投递时记录里会出现好几条一样的，
            # 模型看到会以为「对方说了好几遍」，甚至在回复里说出来。
            if line in seen_lines:
                continue
            seen_lines.add(line)
            lines.append(line)
        if not lines:
            return atmosphere_hint
        body = "\n".join(lines)
        max_total = max(200, self._cfg_int("context_max_chars", 1200))
        if len(body) > max_total:
            # 保留最近的上下文（列表末尾才是最近的对话）
            body = "…\n" + body[-max_total:]
        hint_block = f"{atmosphere_hint}\n" if atmosphere_hint else ""
        naming_rule = ""
        if peer_name:
            naming_rule = (
                f"对话对象的昵称是「{peer_name}」，请用这个昵称称呼对方，"
                "不要用「亲爱的」「宝贝」这类泛称，也不要编造别的称呼。\n"
            )
        return (
            "<chat_context>\n"
            "以下是当前会话最近的对话记录，请据此理解上下文，"
            "不要逐条复述，也不要提及这些记录本身：\n"
            f"{body}\n"
            "</chat_context>\n"
            f"{hint_block}"
            "<reply_hint>\n"
            "你现在是主动参与这段对话（对方并没有点名你），"
            "请像真人一样自然地接话，回复要简短，不要显得突兀或莫名其妙。"
            "也要顾及当前群里的气氛，别抢话、别在气氛紧张时插嘴。\n"
            f"{naming_rule}"
            "</reply_hint>"
        )

    # ------------------------------------------------------------------
    # 被动上下文采集
    # ------------------------------------------------------------------
    def capture_message_background(self, event: AstrMessageEvent) -> None:
        """被动记录收到的消息，供上下文感知使用（不阻塞消息处理）。"""
        if not self._cfg_bool("capture_context_enabled", True):
            return
        if not self._cfg_bool("enabled", True):
            return
        content = (event.message_str or "").strip()
        if not content:
            return
        try:
            if str(event.get_sender_id() or "") == str(event.get_self_id() or ""):
                return
        except Exception:
            pass
        if not self._should_track(event):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - 非异步环境
            return
        task = loop.create_task(self._capture_message(event, content))
        task.add_done_callback(self._on_capture_done)

    # 用户消息的入库去重窗口（秒）：同一会话、同一发送者、同一内容在这个
    # 窗口内只会入库一次，防止一句话被记成两三句。
    _USER_DEDUPE_WINDOW = 30.0

    async def _record_user_message(
        self,
        session_key: str,
        content: str,
        sender: str = "",
        sender_id: str = "",
    ) -> bool:
        """写入一条用户消息，短时间内重复的同一条内容会被忽略。"""
        # 顺手更新昵称缓存：白名单要能按昵称匹配，而过滤器是同步的
        if sender_id and sender:
            names = self._latest_names.setdefault(session_key, {})
            names[str(sender_id)] = str(sender)
        try:
            return bool(
                await self.store.add_message(
                    session_key,
                    "user",
                    content,
                    sender,
                    sender_id,
                    dedupe_window=self._USER_DEDUPE_WINDOW,
                )
            )
        except Exception as exc:  # pragma: no cover
            logger.warning(f"[主动陪伴] 写入用户消息失败: {exc}")
            return False

    def _on_capture_done(self, task: Any) -> None:
        try:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                logger.warning(f"[主动陪伴] 采集消息失败: {exc}")
        except Exception:  # pragma: no cover
            pass

    async def _capture_message(self, event: AstrMessageEvent, content: str) -> None:
        umo = event.unified_msg_origin
        sender = ""
        sender_id = ""
        try:
            sender = event.get_sender_name() or ""
            sender_id = str(event.get_sender_id() or "")
        except Exception:
            sender = ""
        await self.store.ensure_session(
            umo,
            umo,
            event.get_platform_name() or "",
            self._chat_type(event),
            self._session_title(event),
        )
        self._tracked_sessions.add(umo)
        await self._record_user_message(umo, content, sender, sender_id)
        await self._update_affection(
            umo, sender_id, sender, content, event="user_message"
        )
        try:
            event.set_extra(_CAPTURED_FLAG, True)
        except Exception:  # pragma: no cover
            pass
        await self._maybe_refresh_atmosphere(umo)

    # ------------------------------------------------------------------
    # 气氛感知
    # ------------------------------------------------------------------
    async def _maybe_refresh_atmosphere(self, session_key: str) -> None:
        """按节流刷新某个会话的气氛。"""
        if not self._cfg_bool("atmosphere_enabled", True):
            return
        now = time.time()
        refresh_minutes = max(0.5, self._cfg_float("atmosphere_refresh_minutes", 3))
        last = self._atmosphere_refreshed_at.get(session_key, 0.0)
        if now - last < refresh_minutes * 60:
            return
        self._atmosphere_refreshed_at[session_key] = now
        try:
            window = max(1, self._cfg_int("atmosphere_window_minutes", 30))
            messages = await self.store.recent_messages(session_key, window * 2)
            state = atmosphere.analyze(
                messages,
                now=now,
                window_minutes=window,
                cold_rate=self._cfg_float("atmosphere_cold_rate", 0.12),
                hot_rate=self._cfg_float("atmosphere_hot_rate", 0.8),
                conflict_ratio=self._cfg_float("atmosphere_conflict_ratio", 0.15),
            )
            self._atmosphere_cache[session_key] = state
            await self.store.update_atmosphere(session_key, state.to_dict())
        except Exception as exc:  # pragma: no cover
            logger.debug(f"[主动陪伴] 刷新气氛失败: {exc}")

    async def _refresh_atmospheres(self) -> None:
        """为所有被跟踪的会话刷新气氛。"""
        if not self._cfg_bool("atmosphere_enabled", True):
            return
        for session_key in list(self._tracked_sessions):
            try:
                await self._maybe_refresh_atmosphere(session_key)
            except Exception:  # pragma: no cover
                continue

    def _atmosphere_of(self, session_key: str) -> Any:
        """同步读取缓存的气氛（没有缓存时返回 None）。"""
        if not self._cfg_bool("atmosphere_enabled", True):
            return None
        return self._atmosphere_cache.get(session_key)

    def _atmosphere_levels(self) -> dict[str, int]:
        """统计各气氛等级的会话数量。"""
        levels: dict[str, int] = {}
        for state in self._atmosphere_cache.values():
            level = getattr(state, "level", "未知")
            levels[level] = levels.get(level, 0) + 1
        return levels

    def _atmosphere_hint(self, session_key: str) -> str:
        """给提示词用的一句话气氛提示。"""
        state = self._atmosphere_of(session_key)
        if state is None:
            return ""
        return f"当前气氛：{state.level}。{state.advice}"

    def _should_track(self, event: AstrMessageEvent) -> bool:
        """是否需要记录这个会话的消息。"""
        try:
            umo = event.unified_msg_origin
        except Exception:
            return False
        # 命中黑名单的会话一律不记录：既然不想被主动搭话，
        # 也不希望聊天内容被存下来。
        candidates: set[str] = {str(umo)}
        for getter in ("get_sender_id", "get_group_id", "get_session_id"):
            try:
                candidates.add(str(getattr(event, getter)() or ""))
            except Exception:
                pass
        if ":" in str(umo):
            candidates.add(str(umo).rsplit(":", 1)[-1])
        private_blacklist = self._cfg_list_names("private_proactive_blacklist")
        group_blacklist = self._cfg_list_names("group_proactive_blacklist")
        reply_blacklist = self._cfg_list_names("proactive_reply_blacklist")
        if self._hit_any(private_blacklist, candidates) or self._hit_any(
            group_blacklist, candidates
        ):
            return False
        if self._hit_any(reply_blacklist, candidates):
            return False
        if umo in self._tracked_sessions:
            return True
        whitelist = self._cfg_list_names("proactive_reply_whitelist")
        if not whitelist or "*" in whitelist:
            # 允许所有人主动回复 → 记录所有会话的上下文
            return self._can_track_more()
        if self._hit_any(whitelist, candidates):
            return self._can_track_more()
        return False

    def _can_track_more(self) -> bool:
        """限制被动采集的会话数量，避免在超大群里无限膨胀。"""
        limit = max(1, self._cfg_int("capture_context_max_sessions", 200))
        return len(self._tracked_sessions) < limit

    async def _on_proactive_reply(self, event: AstrMessageEvent) -> None:
        umo = event.unified_msg_origin
        self._proactive_reply_times[umo] = time.time()
        chat_type = self._chat_type(event)
        await self.store.ensure_session(
            umo,
            umo,
            event.get_platform_name() or "",
            chat_type,
            self._session_title(event),
        )
        if not event.get_extra(_CAPTURED_FLAG, False):
            await self._record_user_message(
                umo,
                (event.message_str or "").strip(),
                event.get_sender_name() or "",
                str(event.get_sender_id() or ""),
            )
        sender_id = ""
        try:
            sender_id = str(event.get_sender_id() or "")
        except Exception:
            sender_id = ""
        if sender_id:
            await self._update_affection(
                umo,
                sender_id,
                event.get_sender_name() or "",
                (event.message_str or "").strip(),
                event="proactive_reply",
            )
        # 标记一下：这次回复已经计过好感度与「机器人回应」，
        # 事件收尾时（_record_turn）不要再重复计一次。
        try:
            event.set_extra(_PROACTIVE_REPLY_FLAG, True)
        except Exception:  # pragma: no cover
            pass
        session = await self.store.get_session(umo)
        count = int((session or {}).get("proactive_replies") or 0) + 1
        await self.store.update_session(umo, proactive_replies=count)
        await self.store.add_event(
            "proactive_reply",
            umo,
            {
                "sender": event.get_sender_id() or "",
                "text": (event.message_str or "").strip()[:200],
            },
        )

    def should_proactive_reply(self, event: AstrMessageEvent, cfg: Any) -> bool:
        """判断这条消息是否应该触发主动回复（同步、无 IO）。"""
        allowed, reason = self._evaluate_proactive_reply(event, cfg)
        if allowed:
            self._last_reply_decision = reason
            self._reply_skip_reasons.pop(reason, None)
        else:
            self._last_reply_decision = reason
            self._reply_skip_reasons[reason] = (
                self._reply_skip_reasons.get(reason, 0) + 1
            )
        return allowed

    def _evaluate_proactive_reply(
        self, event: AstrMessageEvent, cfg: Any
    ) -> tuple[bool, str]:
        """返回 (是否主动回复, 未触发的原因)，便于排查。"""
        if not self._cfg_bool("proactive_reply_enabled", True):
            return False, "主动回复开关已关闭"
        if not self._cfg_bool("enabled", True):
            return False, "插件总开关已关闭"
        # 机器人本来就会回复（被 @ / 唤醒词 / 私聊）→ 不重复触发。
        # 注意不能用 is_wake：任何插件的监听器通过过滤器都会把它置 True。
        if getattr(event, "is_at_or_wake_command", False):
            return False, "该消息本来就会唤醒机器人"
        try:
            if str(event.get_sender_id() or "") == str(event.get_self_id() or ""):
                return False, "机器人自己的消息"
        except Exception:
            pass
        content = (event.message_str or "").strip()
        min_length = max(0, self._cfg_int("proactive_reply_min_length", 2))
        if len(content) < min_length:
            return False, f"消息过短（<{min_length}）"
        # 别人之间互相 @ 时不要插嘴（这句话不是对我们说的）
        if self._mentions_other(event):
            return False, "消息在 @ 其他人"
        if self._looks_like_command(content, cfg):
            return False, "指令消息"
        # 免打扰与「允许主动发言」开关要和定时主动消息保持一致，
        # 否则关掉了私聊免打扰，私聊里对方一说话插件还是会主动接话。
        chat_type = self._chat_type(event)
        if chat_type == "group" and not self._cfg_bool("group_enabled", True):
            return False, "群聊主动发言已关闭"
        if chat_type == "private" and not self._cfg_bool("private_enabled", True):
            return False, "私聊主动发言已关闭"
        quiet = self._cfg_list("quiet_hours", [])
        if chat_type == "private":
            private_quiet = self._cfg_list("private_quiet_hours", [])
            if private_quiet:
                quiet = private_quiet
        if dnd.in_quiet_hours(_dt.datetime.now(), quiet):
            return False, "处于免打扰时段"
        if not self._in_reply_whitelist(event):
            return False, "不在主动回复白名单内"
        if self._in_reply_blacklist(event):
            return False, "在主动回复黑名单内"
        umo = event.unified_msg_origin
        cooldown = max(0, self._cfg_int("proactive_reply_cooldown_minutes", 10))
        last = self._proactive_reply_times.get(umo, 0.0)
        if cooldown > 0 and (time.time() - last) < cooldown * 60:
            return False, f"冷却中（{cooldown} 分钟）"
        probability = self._cfg_float("proactive_reply_probability", 0.2)
        probability = max(0.0, min(1.0, probability))
        if probability <= 0.0:
            return False, "回复概率为 0"
        # 读空气：气氛影响触发概率
        state = self._atmosphere_of(umo)
        if state is not None:
            factor = atmosphere.probability_factor(
                state, boost=self._cfg_float("atmosphere_boost", 1.5)
            )
            if factor <= 0.0:
                return False, f"气氛{state.level}，不插嘴"
            if probability < 1.0:
                probability = min(1.0, probability * factor)
        # 好感度越高，越愿意主动搭话
        # （概率配成 1.0 表示「总是回复」，此时不再做软调节）
        if probability < 1.0 and self._cfg_bool("affection_enabled", True):
            try:
                sender_id = str(event.get_sender_id() or "")
            except Exception:
                sender_id = ""
            score = self._affection_cached(umo, sender_id)
            probability = min(1.0, probability * affection.probability_factor(score))
        if probability < 1.0 and random.random() >= probability:
            return False, f"未命中概率（{probability:.2f}）"
        # 同一条消息（message_id 相同）只允许触发一次主动回复。
        # 平台偶尔会把同一条消息重复投递给插件，如果不去重，
        # 就会在群里连着发好几条一模一样的回复。
        mid = self._message_id(event)
        if mid:
            if mid in self._reply_triggered_message_ids:
                return False, "这条消息已经触发过主动回复"
            self._reply_triggered_message_ids[mid] = time.time()
            self._prune_reply_triggered_ids()
        # 判定通过时就立刻占用冷却时间戳：handler 是异步执行的，
        # 如果等到 handler 里才记，同一条消息被重复投递时会连续判定通过。
        self._proactive_reply_times[umo] = time.time()
        return True, "命中"

    def _dedupe_request_contexts(self, req: Any) -> int:
        """把 LLM 请求里的会话历史去重：相同角色 + 相同内容只保留一条。

        平台偶尔会把同一条消息重复投递给 AstrBot，历史里就会留下好几条
        一模一样的记录，模型看到后会以为「对方连着说了三遍」，甚至在回复里
        直接说出来。这里在请求发出前把它们合并掉。

        返回被去掉的条数；结构不认识时原样返回，不做任何修改。
        """
        if not self._cfg_bool("dedupe_llm_context_enabled", True):
            return 0
        contexts = getattr(req, "contexts", None)
        if not isinstance(contexts, list) or len(contexts) < 2:
            return 0

        def _key(item: Any) -> tuple[str, str]:
            if isinstance(item, dict):
                role = str(item.get("role") or "")
                content = item.get("content")
            else:
                role = str(getattr(item, "role", "") or "")
                content = getattr(item, "content", None)
            if isinstance(content, list):
                # 多模态内容：取一个稳定的字符串表示
                try:
                    content = json.dumps(content, ensure_ascii=False, sort_keys=True)
                except Exception:  # pragma: no cover
                    content = str(content)
            text = str(content or "").strip()
            return role, text

        seen: set[tuple[str, str]] = set()
        kept: list[Any] = []
        removed = 0
        # 当前这一轮的 prompt 会单独发给模型；如果历史里已经有内容完全一样的
        # 那条用户消息，就会被模型数成两次（平台再重复投递一次就成了三次）。
        # 这种情况下保留 prompt 那一次，把历史里的重复条目去掉。
        prompt_text = str(getattr(req, "prompt", "") or "").strip()
        for item in contexts:
            key = _key(item)
            if not key[1]:
                # 没有内容的条目（例如空的占位）直接丢掉
                removed += 1
                continue
            if prompt_text and key[0] == "user" and key[1] == prompt_text:
                removed += 1
                continue
            # 只删**相邻**的重复项：平台重复投递时同一条消息会连着出现好几次。
            # 不做全局去重，否则用户隔几轮又说了一遍同样的话也会被删掉，
            # 对话历史就被篡改了。
            if key in seen and kept and _key(kept[-1]) == key:
                removed += 1
                continue
            seen.add(key)
            kept.append(item)
        if removed and len(kept) > 0:
            try:
                req.contexts = kept
            except Exception:  # pragma: no cover - 只读属性时跳过
                return 0
        return removed

    @staticmethod
    def _message_id(event: AstrMessageEvent) -> str:
        """取消息唯一 ID（平台不一定提供，取不到就返回空串）。"""
        try:
            getter = getattr(event, "get_message_id", None)
            if callable(getter):
                return str(getter() or "")
        except Exception:  # pragma: no cover
            pass
        return ""

    def _prune_reply_triggered_ids(self) -> None:
        """清理过期的「已触发主动回复的消息 ID」，避免集合无限增长。"""
        if len(self._reply_triggered_message_ids) < 500:
            return
        # 按时间淘汰最早的，而不是整表清空——整表清空会让旧的消息 ID
        # 重新变得「没触发过」，老问题就复发 了。
        cutoff = time.time() - 3600.0
        stale = [
            mid for mid, ts in self._reply_triggered_message_ids.items() if ts < cutoff
        ]
        for mid in stale:
            self._reply_triggered_message_ids.pop(mid, None)
        # 还是太多（短时间内触发了很多次）就整表清空
        if len(self._reply_triggered_message_ids) >= 500:
            self._reply_triggered_message_ids.clear()

    def _looks_like_command(self, content: str, cfg: Any) -> bool:
        """判断消息是否是指令（指令消息不参与主动回复）。"""
        if content.startswith("/"):
            return True
        prefixes: list[str] = []
        try:
            if cfg is not None:
                raw = cfg.get("wake_prefix") or []
                if isinstance(raw, str):
                    raw = [raw]
                prefixes = [str(p) for p in raw if str(p)]
        except Exception:
            prefixes = []
        return any(prefix and content.startswith(prefix) for prefix in prefixes)

    def _reply_name_candidates(self, event: AstrMessageEvent) -> set[str]:
        """收集主动回复名单判定时可用的标识：发送者 ID / 昵称 / 会话 ID / 群号 / umo。"""
        candidates: set[str] = set()
        try:
            candidates.add(str(event.get_sender_id() or ""))
        except Exception:
            pass
        try:
            candidates.add(str(event.get_group_id() or ""))
        except Exception:
            pass
        try:
            candidates.add(str(event.get_session_id() or ""))
        except Exception:
            pass
        try:
            candidates.add(str(event.unified_msg_origin or ""))
        except Exception:
            pass
        # 昵称：只匹配**当前发送者**的最新昵称。
        # （不能把会话里所有人的昵称都算进来，否则填了 A 的昵称会让 B 也能触发。）
        try:
            session_key = str(event.unified_msg_origin or "")
            names = self._latest_names.get(session_key) or {}
            sender_id = str(event.get_sender_id() or "")
            if sender_id and names.get(sender_id):
                candidates.add(str(names[sender_id]))
        except Exception:  # pragma: no cover
            pass
        return {item for item in candidates if item}

    def _in_reply_whitelist(self, event: AstrMessageEvent) -> bool:
        """主动回复白名单判定：命中发送者 ID / 昵称 / 会话 ID / 群号 / umo 之一即可。

        白名单为空表示不做额外限制（谁都可以触发主动回复）；
        填 `*` 语义相同。填写具体值后，只有命中者才会触发。

        与私聊白名单保持一致：**忽略大小写**，并且支持用昵称匹配
        （昵称取该会话里最新出现过的那个，改了昵称也不会失效）。
        """
        whitelist = self._cfg_list_names("proactive_reply_whitelist")
        if not whitelist or "*" in whitelist:
            return True
        return self._hit_any(whitelist, self._reply_name_candidates(event))

    def _in_reply_blacklist(self, event: AstrMessageEvent) -> bool:
        """主动回复黑名单判定：命中就不主动回复（优先于白名单）。"""
        blacklist = self._cfg_list_names("proactive_reply_blacklist")
        if not blacklist:
            return False
        return self._hit_any(blacklist, self._reply_name_candidates(event))

    @filter.on_decorating_result()
    async def capture_turn(self, event: AstrMessageEvent) -> None:
        """在机器人发送回复前记录这一轮对话，用于上下文感知与情绪计算。

        这里刻意 **不使用** `event_message_type(ALL)` 监听器：任何通过过滤器的
        handler 都会把消息标记为 wake，导致 Bot 对群里每条消息都做出回复。
        回复前钩子只在机器人本来就要说话时触发，既能拿到上下文，又不会打扰。
        """
        try:
            await self._record_turn(event)
        except Exception as exc:  # pragma: no cover - 绝不影响正常对话
            logger.warning(f"[主动陪伴] 记录对话失败: {exc}")

    async def _record_turn(self, event: AstrMessageEvent) -> None:
        content = (event.message_str or "").strip()
        reply = ""
        try:
            result = event.get_result()
            if result is not None and getattr(result, "chain", None):
                parts = []
                for comp in result.chain:
                    if str(getattr(comp, "type", "")).lower() == "plain":
                        parts.append(str(getattr(comp, "text", "")))
                reply = "".join(parts).strip()
        except Exception:
            reply = ""

        if not content and not reply:
            return

        umo = event.unified_msg_origin

        # LLM 网关把输入判为敏感时会整条失败（All chat models failed / sensitive）。
        # 这种错误如果反复出现，说明注入的上下文里有内容被拦了——先熔断本会话的
        # 上下文注入，让正常回复立刻恢复，再把原因记进事件日志。
        if reply and self._looks_like_llm_rejection(reply):
            await self.store.add_event(
                "llm_input_rejected",
                umo,
                {"reply": reply[:200], "at": _now_str()},
            )
            try:
                if not await self._context_inject_disabled(umo):
                    await self._set_context_inject(umo, True)
                    logger.warning(
                        f"[主动陪伴] 会话 {umo} 的上下文注入已自动熔断"
                        "（LLM 输入被安全拦截），可用 `/陪伴 上下文 恢复` 重新开启。"
                    )
            except Exception as exc:  # pragma: no cover
                logger.warning(f"[主动陪伴] 熔断上下文注入失败: {exc}")
            # 错误提示不是正常回复，不要写进聊天记录
            reply = ""

        chat_type = self._chat_type(event)
        title = self._session_title(event)
        platform = event.get_platform_name() or ""
        await self.store.ensure_session(umo, umo, platform, chat_type, title)

        sender_name = event.get_sender_name() or ""
        sender_id = ""
        try:
            sender_id = str(event.get_sender_id() or "")
        except Exception:
            sender_id = ""
        now = time.time()
        update: dict[str, Any] = {"last_msg_at": now}
        if self._mentions_bot(event):
            note = self._load_note(await self.store.get_session(umo))
            note["last_mention_at"] = now
            update["note"] = json.dumps(note, ensure_ascii=False)

        if content and not event.get_extra(_CAPTURED_FLAG, False):
            # 被动采集已经记录过这条消息时不要重复入库
            await self._record_user_message(umo, content, sender_name, sender_id)
        if reply:
            await self.store.add_message(
                umo, "bot", reply, "bot", "", dedupe_window=10.0
            )
            # 主动回复已经在 _on_proactive_reply 里计过好感度与回应次数，
            # 这里再计一次会让「机器人回应 N 次」与好感度都翻倍。
            already_counted = bool(event.get_extra(_PROACTIVE_REPLY_FLAG, False))
            if sender_id and not already_counted:
                await self._update_affection(
                    umo, sender_id, sender_name, event="bot_reply"
                )
        await self.store.update_session(umo, **update)

        # 写完立刻裁剪本会话的旧记录，这样「每会话保留消息数」改完马上生效，
        # 不必等定时任务跑一轮（数据库也不会被旧聊天撑大）。
        try:
            keep = max(10, self._cfg_int("keep_messages_per_session", 200))
            await self.store.prune_session_messages(umo, keep)
            await self.store.prune_session_messages_by_age(
                umo, self._cfg_int("keep_messages_days", 30)
            )
        except Exception as exc:  # pragma: no cover
            logger.warning(f"[主动陪伴] 裁剪会话消息失败: {exc}")

        if content and self._cfg_bool("mood_enabled", True):
            await self._apply_mood_event(umo, content)

    async def _update_affection(
        self,
        session_key: str,
        user_id: str,
        nickname: str,
        content: str = "",
        event: str = "user_message",
    ) -> None:
        """根据一次互动更新好感度。"""
        if not user_id:
            return
        if not self._cfg_bool("affection_enabled", True):
            return
        try:
            positive = False
            negative = False
            if content:
                dv, _da = emotion.analyze_text(content)
                positive = dv > 0.2
                negative = dv < -0.2
            await self.store.touch_profile(
                session_key,
                user_id,
                nickname=nickname,
                event=event,
                positive=positive,
                negative=negative,
            )
            profile = await self.store.get_profile(session_key, user_id)
            if profile:
                self._affection_cache[self._affection_key(session_key, user_id)] = (
                    float(profile.get("affection") or 0.0)
                )
        except Exception as exc:  # pragma: no cover
            logger.debug(f"[主动陪伴] 更新好感度失败: {exc}")

    @staticmethod
    def _affection_key(session_key: str, user_id: str) -> str:
        return f"{session_key}|{user_id}"

    async def _affection_of(self, session_key: str, user_id: str) -> float:
        """读取某个用户当前的好感分（含衰减）。

        以数据库为准，这样手动改过的好感度能立刻生效；
        数据库里没有记录时才回退到内存缓存。
        """
        profile = await self.store.get_profile(session_key, user_id)
        if profile:
            return affection.decayed(
                float(profile.get("affection") or 0.0),
                float(profile.get("affection_at") or 0.0),
            )
        cached = self._affection_cache.get(self._affection_key(session_key, user_id))
        if cached is not None:
            return affection.decayed(cached, 0.0)
        return 0.0

    def _affection_cached(self, session_key: str, user_id: str) -> float:
        """同步读取好感分（过滤器用）。"""
        if not user_id:
            return 0.0
        return self._affection_cache.get(self._affection_key(session_key, user_id), 0.0)

    def _relationship_hint(self, session_key: str, user_id: str) -> str:
        """给模型的一句话关系提示。"""
        if not user_id:
            return ""
        score = self._affection_cached(session_key, user_id)
        return affection.relationship_hint(score)

    def _mentions_other(self, event: AstrMessageEvent) -> bool:
        """判断消息是否在 @ 别人（不是 @ 机器人）。

        群里大家互相 @ 时，这句话不是对我们说的，主动回复应该让位。
        """
        try:
            self_id = str(event.get_self_id() or "")
            for comp in getattr(event.message_obj, "message", []) or []:
                if str(getattr(comp, "type", "")).lower() != "at":
                    continue
                qq = str(getattr(comp, "qq", "") or "")
                if not qq or qq == "all":
                    # @ 全体成员属于对所有人说话，框架本身会唤醒机器人
                    continue
                if qq != self_id:
                    return True
        except Exception:
            return False
        return False

    def _mentions_bot(self, event: AstrMessageEvent) -> bool:
        """判断消息中是否 @ 了机器人。"""
        try:
            self_id = str(event.get_self_id() or "")
            if not self_id:
                return False
            for comp in getattr(event.message_obj, "message", []) or []:
                # ComponentType.At 的值为 "At"
                if str(getattr(comp, "type", "")).lower() != "at":
                    continue
                qq = str(getattr(comp, "qq", "") or "")
                if qq == self_id:
                    return True
        except Exception:
            return False
        return False

    async def _apply_mood_event(self, session_key: str, content: str) -> None:
        session = await self.store.get_session(session_key)
        if not session:
            return
        state = self._session_mood(session)
        dv, da = emotion.analyze_text(content)
        # 对方主动搭话，唤醒度略微上升
        da += 0.03
        new_state = emotion.apply_delta(state, dv, da)
        await self.store.update_session(
            session_key,
            valence=new_state.valence,
            arousal=new_state.arousal,
            mood_updated_at=new_state.updated_at,
        )

    # ------------------------------------------------------------------
    # 情绪工具
    # ------------------------------------------------------------------
    def _session_mood(self, session: dict[str, Any]) -> emotion.EmotionState:
        state = emotion.EmotionState(
            valence=float(session.get("valence") or 0.0),
            arousal=float(session.get("arousal") or emotion.BASE_AROUSAL),
            updated_at=float(session.get("mood_updated_at") or 0.0),
        )
        if not self._cfg_bool("mood_enabled", True):
            return emotion.EmotionState()
        return emotion.decay(
            state,
            rate=max(0.0, self._cfg_float("mood_decay_per_hour", 0.15)),
        )

    def _load_note(self, session: dict[str, Any] | None) -> dict[str, Any]:
        if not session:
            return {}
        raw = session.get("note") or ""
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (TypeError, ValueError):
            return {}

    # ------------------------------------------------------------------
    # 主动消息调度
    # ------------------------------------------------------------------
    async def tick(self) -> None:
        """执行一轮主动消息检查。"""
        self._last_tick_at = time.time()
        self._last_tick_error = ""
        if not self._cfg_bool("enabled", True):
            return
        sessions = await self.store.list_sessions()
        now = time.time()
        max_per_tick = max(1, self._cfg_int("max_proactive_per_tick", 3))
        sent_count = 0
        # 私聊优先、群聊随后，各自内部随机打散
        sessions = self._order_sessions(sessions)
        for session in sessions:
            if sent_count >= max_per_tick:
                break
            try:
                if await self._maybe_proactive(session, now):
                    sent_count += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_tick_error = str(exc)
                logger.warning(
                    f"[主动陪伴] 会话 {session.get('session_key')} 主动消息失败: {exc}"
                )
        await self._maybe_cleanup()

    async def _maybe_proactive(self, session: dict[str, Any], now: float) -> bool:
        """判断并（必要时）向某个会话发送一条主动消息。"""
        session_key = str(session.get("session_key") or "")
        if not session_key:
            return False
        if not int(session.get("enabled") or 0):
            return False

        chat_type = str(session.get("chat_type") or "")
        is_private = chat_type == "private"
        if chat_type == "group" and not self._cfg_bool("group_enabled", True):
            return False
        if is_private and not self._cfg_bool("private_enabled", True):
            return False
        if is_private and not await self._private_whitelisted(session):
            return False
        if chat_type == "group" and not await self._group_whitelisted(session):
            return False

        limits = self._effective_limits(chat_type)
        quiet = limits["quiet_hours"]
        if dnd.in_quiet_hours(_dt.datetime.now(), quiet):
            return False

        # 每日次数重置
        today = _dt.date.today().isoformat()
        daily_count = int(session.get("daily_count") or 0)
        if str(session.get("daily_date") or "") != today:
            daily_count = 0
            await self.store.update_session(
                session_key, daily_count=0, daily_date=today
            )
        max_daily = int(limits["max_daily"])
        if max_daily > 0 and daily_count >= max_daily:
            return False

        last_msg_at = float(session.get("last_msg_at") or 0.0)
        last_proactive_at = float(session.get("last_proactive_at") or 0.0)
        anchor = max(last_msg_at, last_proactive_at)

        base_idle = float(limits["min_idle"])
        min_idle = self._idle_threshold_minutes(base_idle)
        if anchor:
            idle_minutes = (now - anchor) / 60.0
        else:
            # 从没记录到过任何消息（新建的会话，或刚被清空过记忆）：
            # 按「已经沉默了很久」处理，否则这些会话永远等不到主动搭话。
            created_at = float(session.get("created_at") or 0.0)
            idle_minutes = (now - created_at) / 60.0 if created_at else 1e6
        # 读空气：群正热闹或气氛紧张时不要硬插话。
        # 私聊是两个人的对话，气氛判定本来就没意义（还可能因为一句气话被判成「紧张」，
        # 结果私聊里彻底闭嘴），所以默认不套用群气氛规则。
        skip_atmosphere = is_private and self._cfg_bool("private_skip_atmosphere", True)
        # 气氛数据照常刷新（面板要显示），但私聊不用它当门控
        state = self._atmosphere_of(session_key)
        if state is not None and not skip_atmosphere:
            if not atmosphere.allow_proactive_message(state):
                return False
            if state.level == atmosphere.LEVEL_COLD:
                # 冷场时允许更早一点暖场
                min_idle = min_idle * 0.5
        if idle_minutes < min_idle:
            return False

        # 情绪影响发言频率：心情低落时收敛一些，心情好时更积极。
        # （此前这个配置项没有任何实现，属于「面板能改但没效果」的死配置。）
        if self._cfg_bool("mood_affect_frequency", True):
            mood_now = self._session_mood(session)
            if mood_now.valence < -0.2:
                # 低落：要求再多沉默一会儿才开口
                min_idle *= 1.5
                if idle_minutes < min_idle:
                    return False
            elif mood_now.valence > 0.3:
                # 心情好：允许稍微早一点
                min_idle *= 0.8

        cooldown = int(limits["cooldown"])

        if last_proactive_at and (now - last_proactive_at) / 60.0 < cooldown:
            return False

        # 群里要求必须被 @ 过才主动说话
        if (
            chat_type == "group"
            and self._cfg_bool("mention_only_in_group", False)
            and not self._mentioned_recently(session, now)
        ):
            return False

        mood = self._session_mood(session)
        text = await self._compose_message(
            session, mood, idle_minutes, now, manual=False
        )
        if not text:
            return False

        sent = await self._deliver(session_key, text)
        if not sent:
            return False

        await self.store.add_message(session_key, "proactive", text, "bot")
        await self.store.update_session(
            session_key,
            last_proactive_at=now,
            messages_sent=int(session.get("messages_sent") or 0) + 1,
            daily_count=daily_count + 1,
            daily_date=today,
        )
        if self._cfg_bool("mood_enabled", True):
            # 主动搭话成功，心情略微变好
            new_mood = emotion.apply_delta(mood, 0.04, -0.02)
            await self.store.update_session(
                session_key,
                valence=new_mood.valence,
                arousal=new_mood.arousal,
                mood_updated_at=time.time(),
            )
        await self.store.add_event(
            "proactive_sent",
            session_key,
            {"text": text, "mood": emotion.label_for(mood)},
        )
        return True

    def _order_sessions(self, sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """调度顺序：私聊优先，其次群聊；每一类内部随机打散。

        私聊只有一个人等着回话，应该优先占用本轮发言名额；打散是为了避免
        每次都从同一个会话开始，形成规律性的打扰。
        """
        random.shuffle(sessions)
        if not self._cfg_bool("private_priority", True):
            return sessions
        private = [s for s in sessions if str(s.get("chat_type") or "") == "private"]
        others = [s for s in sessions if str(s.get("chat_type") or "") != "private"]
        random.shuffle(private)
        random.shuffle(others)
        return private + others

    def _cfg_list_names(self, key: str) -> list[str]:
        """读取一个名单类配置（去掉空项）。"""
        return [
            str(item).strip() for item in self._cfg_list(key, []) if str(item).strip()
        ]

    @staticmethod
    def _hit_any(items: list[str], candidates: set[str]) -> bool:
        """名单是否命中候选集合（忽略大小写）。"""
        if not items:
            return False
        lowered = {item.lower() for item in candidates if item}
        return any(item in candidates or item.lower() in lowered for item in items)

    async def _private_whitelisted(self, session: dict[str, Any]) -> bool:
        """私聊主动搭话名单判定。

        - **黑名单优先**：命中黑名单的人，私聊不会被主动搭话；
        - 留空（或填 `*`）= 不限制，任何人的私聊都可以被主动搭话；
        - 填了白名单之后，**只有命中的人**的私聊才会被主动搭话，其余人的私聊
          保持安静（群聊完全不受影响）。

        可填：对方用户 ID、昵称、会话 ID（umo）。昵称按「手动昵称 > 平台昵称」
        取最新那个，所以改了昵称也不会失效。
        """
        blacklist = self._cfg_list_names("private_proactive_blacklist")
        whitelist = self._cfg_list_names("private_proactive_whitelist")
        if not blacklist and (not whitelist or "*" in whitelist):
            return True
        session_key = str(session.get("session_key") or "")
        candidates: set[str] = {
            session_key,
            str(session.get("umo") or ""),
        }
        # 会话键形如「平台:类型:ID」，末段就是群号 / 用户 ID，
        # 这样直接填「12345」也能命中，不用先知道完整的会话键。
        if ":" in session_key:
            candidates.add(session_key.rsplit(":", 1)[-1])
        try:
            names, peer_id, peer_name = await self._nickname_context(session_key)
        except Exception:  # pragma: no cover
            names, peer_id, peer_name = {}, "", ""
        if peer_id:
            candidates.add(peer_id)
        if peer_name:
            candidates.add(peer_name)
        candidates.update(str(uid) for uid in names)
        # 黑名单优先
        if self._hit_any(blacklist, candidates):
            return False
        if not whitelist or "*" in whitelist:
            return True
        return self._hit_any(whitelist, candidates)

    async def _group_whitelisted(self, session: dict[str, Any]) -> bool:
        """群聊主动搭话名单判定（与私聊同样的黑名单优先规则）。

        可填：群号、群会话 ID（umo）。留空或填 `*` = 不限制。
        """
        blacklist = self._cfg_list_names("group_proactive_blacklist")
        whitelist = self._cfg_list_names("group_proactive_whitelist")
        if not blacklist and (not whitelist or "*" in whitelist):
            return True
        candidates: set[str] = {
            str(session.get("session_key") or ""),
            str(session.get("umo") or ""),
            str(session.get("title") or ""),
        }
        # 会话键末段就是群号，方便直接填「12345」
        session_key = str(session.get("session_key") or "")
        if ":" in session_key:
            candidates.add(session_key.rsplit(":", 1)[-1])
        if self._hit_any(blacklist, candidates):
            return False
        if not whitelist or "*" in whitelist:
            return True
        return self._hit_any(whitelist, candidates)

    def _effective_limits(self, chat_type: str) -> dict[str, Any]:
        """按会话类型算出**实际生效**的阈值。

        私聊有自己的一套默认值（比群里更主动），对应的配置项设为 0 / 留空时
        自动回退到全局设置，因此群聊行为完全不受影响。
        """
        is_private = chat_type == "private"
        max_daily = self._cfg_int("max_daily_messages", 6)
        base_idle = float(self._cfg_int("min_idle_minutes", 45))
        cooldown = self._cfg_int("cooldown_minutes", 180)
        quiet = self._cfg_list("quiet_hours", [])
        if is_private:
            private_daily = self._cfg_int("private_max_daily_messages", 10)
            if private_daily > 0:
                max_daily = private_daily
            private_idle = self._cfg_int("private_min_idle_minutes", 15)
            if private_idle > 0:
                base_idle = float(private_idle)
            private_cooldown = self._cfg_int("private_cooldown_minutes", 60)
            if private_cooldown > 0:
                cooldown = private_cooldown
            private_quiet = self._cfg_list("private_quiet_hours", [])
            if private_quiet:
                quiet = private_quiet
        return {
            "max_daily": max_daily,
            "min_idle": base_idle,
            "cooldown": cooldown,
            "quiet_hours": quiet,
        }

    def _idle_threshold_minutes(self, base: float | None = None) -> float:
        """静默阈值，带随机抖动，避免所有会话在同一时刻一起触发。"""
        if base is None:
            base = float(self._cfg_int("min_idle_minutes", 45))
        base = float(base)
        ratio = max(0.0, self._cfg_float("idle_jitter_ratio", 0.2))
        if base <= 0 or ratio <= 0:
            return base
        jitter = random.uniform(-ratio, ratio)
        return max(0.0, base * (1.0 + jitter))

    def _mentioned_recently(self, session: dict[str, Any], now: float) -> bool:
        note = self._load_note(session)
        last_mention = float(note.get("last_mention_at") or 0.0)
        if not last_mention:
            return False
        # 只在最近一次被 @ 之后的窗口内有效
        window = max(self._cfg_int("min_idle_minutes", 45), 10) * 60.0
        return (now - last_mention) <= window

    async def _maybe_cleanup(self) -> None:
        """定期清理过期的消息与事件日志。"""
        self._cleanup_counter = getattr(self, "_cleanup_counter", 0) + 1
        if self._cleanup_counter % 10 != 1:
            return
        try:
            await self.store.prune_messages(
                max(10, self._cfg_int("keep_messages_per_session", 200))
            )
            # 按保留天数清理过期聊天记录（0 = 永不按时间清理）
            removed = await self.store.prune_messages_by_age(
                self._cfg_int("keep_messages_days", 30)
            )
            if removed:
                logger.info(f"[主动陪伴] 已清理 {removed} 条过期聊天记录")
            await self.store.prune_events(max(50, self._cfg_int("keep_events", 1000)))
        except Exception as exc:  # pragma: no cover
            logger.warning(f"[主动陪伴] 清理历史数据失败: {exc}")

    async def _compose_message(
        self,
        session: dict[str, Any],
        mood: emotion.EmotionState,
        idle_minutes: float,
        now: float,
        manual: bool = False,
    ) -> str:
        """结合上下文生成一条主动消息。"""
        session_key = str(session.get("session_key") or "")
        umo = str(session.get("umo") or session_key)
        chat_type = str(session.get("chat_type") or "")

        recent = await self.store.recent_messages(
            session_key, self._cfg_int("max_context_messages", 12) + 8
        )
        # 同样做裁剪与屏蔽词过滤，避免历史消息把整次请求判成敏感
        recent = self._sanitize_messages(recent)
        history = await self._astro_history(umo)
        names, _, peer_name = await self._nickname_context(session_key)
        context_block = generator.build_context_block(
            recent,
            history,
            max_items=self._cfg_int("max_context_messages", 12),
            names=names,
        )
        prompt = generator.build_prompt(
            context_block,
            mood,
            idle_minutes,
            is_group=chat_type == "group",
            group_title=str(session.get("title") or ""),
            peer_name=peer_name,
        )
        hint = self._atmosphere_hint(umo)
        if hint:
            prompt = f"{hint}\n\n{prompt}"
        if manual:
            prompt = "用户刚刚主动要求你发一条消息，请自然地回应。\n\n" + prompt

        provider = await self._chat_provider(umo)
        system_prompt = self._cfg_str("system_prompt", generator.DEFAULT_SYSTEM_PROMPT)
        text = await generator.generate_text(
            provider,
            prompt,
            system_prompt,
            mood,
            chat_type == "group",
            session_id=umo,
        )
        return text.strip()

    async def _astro_history(self, umo: str) -> list[str]:
        """读取 AstrBot 自身的会话历史（可能为空）。"""
        try:
            manager = getattr(self.context, "conversation_manager", None)
            if manager is None:
                return []
            cid = await manager.get_curr_conversation_id(umo)
            if not cid:
                return []
            contexts, _pages = await manager.get_human_readable_context(
                umo, cid, page=1, page_size=10
            )
            return [c for c in contexts if isinstance(c, str)]
        except Exception:
            return []

    async def _chat_provider(self, umo: str) -> Any:
        try:
            provider = await self.context.get_using_provider_async(umo)
        except Exception as exc:
            logger.warning(f"[主动陪伴] 获取对话模型失败: {exc}")
            return None
        return provider

    async def _deliver(self, umo: str, text: str) -> bool:
        """发送主动消息（含可选 TTS 语音）。"""
        chain = MessageChain()
        chain.message(text)

        voice_path = await self._make_voice(umo, text)
        if voice_path:
            chain.chain.append(Record(file=voice_path, url=voice_path, text=text))
            if not self._cfg_bool("tts_send_text", True):
                # ComponentType.Plain 的值为 "Plain"
                chain.chain = [
                    comp
                    for comp in chain.chain
                    if str(getattr(comp, "type", "")).lower() != "plain"
                ]
        try:
            await self.context.send_message(umo, chain)
        except Exception as exc:
            logger.warning(f"[主动陪伴] 发送主动消息失败({umo}): {exc}")
            if voice_path:
                # 语音消息发送失败时，退化为纯文本再试一次
                try:
                    await self.context.send_message(umo, MessageChain().message(text))
                    return True
                except Exception as exc2:
                    logger.warning(f"[主动陪伴] 文本兜底发送同样失败({umo}): {exc2}")
            return False
        return True

    async def _make_voice(self, umo: str, text: str) -> str | None:
        """调用 TTS Provider 生成语音文件路径。"""
        if not self._cfg_bool("tts_enabled", False):
            return None
        max_chars = self._cfg_int("tts_max_chars", 120)
        tts_text = text[:max_chars] if max_chars > 0 else text
        provider = None
        provider_id = self._cfg_str("tts_provider_id", "").strip()
        if provider_id:
            try:
                provider = self.context.get_provider_by_id(provider_id)
            except Exception:
                provider = None
        if provider is None:
            try:
                provider = await self.context.get_using_tts_provider_async(umo)
            except Exception as exc:
                logger.warning(f"[主动陪伴] 获取 TTS 模型失败: {exc}")
                provider = None
        if provider is None:
            return None
        try:
            path = await provider.get_audio(tts_text)
        except Exception as exc:
            logger.warning(f"[主动陪伴] TTS 生成失败: {exc}")
            return None
        if not path:
            return None
        try:
            return str(Path(path))
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------
    @filter.command_group("陪伴")
    def companion(self):
        """主动陪伴相关指令"""

    @filter.permission_type(filter.PermissionType.ADMIN)
    @companion.command("开启", alias={"on", "enable", "start"})
    async def companion_enable(self, event: AstrMessageEvent):
        """为当前会话开启主动消息"""
        umo = event.unified_msg_origin
        await self.store.ensure_session(
            umo,
            umo,
            event.get_platform_name() or "",
            self._chat_type(event),
            self._session_title(event),
        )
        await self.store.update_session(umo, enabled=1)
        await self.store.add_event("session_enabled", umo)
        yield event.plain_result("已开启主动陪伴，我会找机会跟你说话的～")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @companion.command("关闭", alias={"off", "disable", "stop"})
    async def companion_disable(self, event: AstrMessageEvent):
        """为当前会话关闭主动消息"""
        umo = event.unified_msg_origin
        await self.store.update_session(umo, enabled=0)
        await self.store.add_event("session_disabled", umo)
        yield event.plain_result("已关闭主动陪伴，我会安静待着的。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @companion.command("状态", alias={"status"})
    async def companion_status(self, event: AstrMessageEvent):
        """查看当前会话的主动陪伴状态"""
        umo = event.unified_msg_origin
        session = await self.store.get_session(umo)
        if not session:
            yield event.plain_result("当前会话还没有主动陪伴的记录。")
            return
        mood = self._session_mood(session)
        last = float(session.get("last_proactive_at") or 0.0)
        last_text = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(last)) if last else "还没有"
        )
        chat_type = str(session.get("chat_type") or self._chat_type(event))
        limits = self._effective_limits(chat_type)
        quiet = limits["quiet_hours"]
        dnd_state = (
            "免打扰中" if dnd.in_quiet_hours(_dt.datetime.now(), quiet) else "可打扰"
        )
        scope_text = "私聊" if chat_type == "private" else "群聊"
        text = (
            f"主动陪伴状态：{'开启' if int(session.get('enabled') or 0) else '关闭'}\n"
            f"当前情绪：{emotion.label_for(mood)}"
            f"（{mood.valence:+.2f} / {mood.arousal:.2f}）\n"
            f"已发送：{int(session.get('messages_sent') or 0)} 条，"
            f"今日：{int(session.get('daily_count') or 0)}/{limits['max_daily']}\n"
            f"上次主动：{last_text}\n"
            f"时段状态：{dnd_state}\n"
            f"{scope_text}阈值：静默 {int(limits['min_idle'])} 分钟 / "
            f"间隔 {int(limits['cooldown'])} 分钟"
        )
        if chat_type == "private":
            wl = self._cfg_list("private_proactive_whitelist", [])
            text += f"\n私聊白名单：{'、'.join(str(w) for w in wl) if wl else '（未设置 = 不限制）'}"
        yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @companion.command("上下文", alias={"context", "ctx"})
    async def companion_context(self, event: AstrMessageEvent):
        """查看 / 开关当前会话的上下文注入"""
        umo = event.unified_msg_origin
        tail = self._cmd_tail(event, "上下文", "context", "ctx").strip()
        await self.store.ensure_session(
            umo,
            umo,
            event.get_platform_name() or "",
            self._chat_type(event),
            self._session_title(event),
        )
        if tail in ("恢复", "开启", "on", "enable"):
            await self._set_context_inject(umo, False)
            await self.store.add_event("context_inject_resumed", umo)
            yield event.plain_result(
                "已恢复本会话的上下文注入。\n"
                "如果还是报「All chat models failed」，可以在插件配置里"
                "把「上下文屏蔽词」填上被拦的关键词，或把「启用上下文注入」关掉。"
            )
            return
        if tail in ("暂停", "关闭", "off", "disable"):
            await self._set_context_inject(umo, True)
            await self.store.add_event("context_inject_paused", umo)
            yield event.plain_result(
                "已暂停本会话的上下文注入（Bot 主动接话时不再附带聊天记录）。"
            )
            return

        disabled = await self._context_inject_disabled(umo)
        global_on = self._cfg_bool("context_injection_enabled", True)
        keywords = self._cfg_list("context_blocked_keywords", [])
        lines = [
            "【上下文注入状态】",
            f"全局开关：{'开' if global_on else '关'}",
            f"当前会话：{'已熔断（不注入）' if disabled else '正常注入'}",
            f"单条上限：{self._cfg_int('context_max_message_chars', 200)} 字",
            f"总长度上限：{self._cfg_int('context_max_chars', 1200)} 字",
            f"屏蔽词：{'、'.join(str(k) for k in keywords) if keywords else '（未设置）'}",
            "指令：`/陪伴 上下文 暂停` 暂停本会话注入，`/陪伴 上下文 恢复` 重新开启。",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @companion.command("说说", alias={"talk", "speak"})
    async def companion_talk(self, event: AstrMessageEvent):
        """立刻触发一条主动消息"""
        umo = event.unified_msg_origin
        await self.store.ensure_session(
            umo,
            umo,
            event.get_platform_name() or "",
            self._chat_type(event),
            self._session_title(event),
        )
        session = await self.store.get_session(umo)
        if not session:
            yield event.plain_result("暂时无法生成消息，请稍后再试。")
            return
        mood = self._session_mood(session)
        text = await self._compose_message(
            session, mood, idle_minutes=0.0, now=time.time(), manual=True
        )
        if not text:
            yield event.plain_result("我暂时没什么想说的～")
            return
        sent = await self._deliver(umo, text)
        if sent:
            await self.store.add_message(
                umo, "proactive", text, "bot", "", dedupe_window=10.0
            )
            # 手动发送也要计入每日额度，否则反复点「说说」可以绕过上限
            today = _dt.date.today().isoformat()
            daily_count = (
                int(session.get("daily_count") or 0)
                if str(session.get("daily_date") or "") == today
                else 0
            )
            await self.store.update_session(
                umo,
                last_proactive_at=time.time(),
                messages_sent=int(session.get("messages_sent") or 0) + 1,
                daily_count=daily_count + 1,
                daily_date=today,
            )
            await self.store.add_event("manual_proactive", umo, {"text": text})
            yield event.plain_result("（已发送）")
        else:
            yield event.plain_result("消息发送失败了，请检查平台是否支持主动消息。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @companion.command("诊断", alias={"debug", "diag"})
    async def companion_diagnose(self, event: AstrMessageEvent):
        """查看主动回复的判定状态，排查为什么没有触发"""
        umo = event.unified_msg_origin
        session = await self.store.get_session(umo)
        lines = [
            "【主动回复诊断】",
            f"开关：{'开' if self._cfg_bool('proactive_reply_enabled', True) else '关'}",
            f"概率：{self._cfg_float('proactive_reply_probability', 0.2)}",
            f"冷却：{self._cfg_int('proactive_reply_cooldown_minutes', 10)} 分钟",
            f"最短长度：{self._cfg_int('proactive_reply_min_length', 2)}",
            f"白名单：{self._cfg_list('proactive_reply_whitelist', []) or '（空 = 不限制）'}",
            f"免打扰：{self._cfg_list('quiet_hours', []) or '（未设置）'}",
            f"上下文采集：{'开' if self._cfg_bool('capture_context_enabled', True) else '关'}"
            f"（已跟踪 {len(self._tracked_sessions)} 个会话）",
            f"上下文注入：{'开' if self._cfg_bool('context_injection_enabled', True) else '关'}"
            f"（单条 ≤{self._cfg_int('context_max_message_chars', 200)} 字，"
            f"总长 ≤{self._cfg_int('context_max_chars', 1200)} 字，"
            f"屏蔽词 {'、'.join(str(k) for k in self._cfg_list('context_blocked_keywords', [])) or '无'}）",
            f"本会话注入：{'已熔断' if await self._context_inject_disabled(umo) else '正常'}",
            f"聊天记录保存：每会话 ≤{self._cfg_int('keep_messages_per_session', 200)} 条，"
            f"{self._cfg_int('keep_messages_days', 30)} 天"
            f"（0 = 不按时间清理）",
            f"最近判定：{self._last_reply_decision}",
        ]
        if self._reply_skip_reasons:
            top = sorted(
                self._reply_skip_reasons.items(), key=lambda kv: kv[1], reverse=True
            )[:3]
            lines.append("跳过原因 TOP3：" + "；".join(f"{k}×{v}" for k, v in top))
        if session:
            lines.append(
                f"本会话：主动回复 {int(session.get('proactive_replies') or 0)} 次，"
                f"记录消息 {len(await self.store.recent_messages(umo, 200))} 条"
            )
        else:
            lines.append("本会话：还没有任何记录（说明消息没被采集到）")
        state = self._atmosphere_of(umo)
        if state is not None:
            lines.append(
                f"气氛：{state.level}（活跃度 {state.score:.2f}，"
                f"情绪 {state.positivity:+.2f}，冲突 {state.conflict:.2f}，"
                f"{state.message_count} 条 / {state.participants} 人）"
            )
            lines.append(f"建议：{state.advice}")
        else:
            lines.append("气氛：暂无数据（读空气已关闭或还没算过）")
        # 画像相关的信息：方便确认「改了为什么没生效」
        try:
            all_profiles = await self.store.list_profiles(None)
        except Exception:  # pragma: no cover
            all_profiles = []
        people = {str(p.get("user_id") or "") for p in all_profiles}
        lines.append("——")
        lines.append(f"插件版本：{self._plugin_version()}")
        lines.append(f"数据库文件：{self.store.db_path}")
        lines.append(
            f"画像：{len(all_profiles)} 行 / {len(people)} 个人"
            f"（同一个人在多个会话里会各有一行，保存时会同步）"
        )
        try:
            self_id = str(event.get_sender_id() or "")
        except Exception:
            self_id = ""
        if self_id:
            rows = await self.store.user_profile_sessions(self_id)
            if rows:
                for row in rows[:3]:
                    lines.append(
                        f"你的画像[{row.get('session_key')}｜{row.get('nickname') or '—'}]："
                        f"好感 {row.get('affection')}｜标签 {row.get('tags') or '[]'}｜"
                        f"备注 {row.get('note') or '—'}｜手动昵称 {row.get('nickname_manual')}"
                    )
            else:
                lines.append("你的画像：暂无记录")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # 指令参数解析
    # ------------------------------------------------------------------
    @staticmethod
    def _cmd_tail(event: AstrMessageEvent, *names: str) -> str:
        """取出「陪伴 <子指令>」之后的参数文本。

        AstrBot 的指令组只做整词前缀匹配，**不会**把子指令名从
        ``event.message_str`` 里剥掉，所以这里按子指令名（含别名）
        定位，取其后的部分作为参数。
        """
        raw = re.sub(r"\s+", " ", (event.message_str or "").strip())
        if not raw:
            return ""
        tokens = raw.split(" ")
        for idx, token in enumerate(tokens):
            if token in names:
                return " ".join(tokens[idx + 1 :]).strip()
        return ""

    @staticmethod
    def _parse_kv(text: str) -> dict[str, str]:
        """解析 ``键=值`` 片段，允许值里带空格。

        例如 ``备注=技术大佬 标签=朋友,技术宅`` →
        ``{"备注": "技术大佬", "标签": "朋友,技术宅"}``
        """
        result: dict[str, str] = {}
        for match in re.finditer(r"([^\s=]+)=(.*?)(?=(?:\s+[^\s=]+=)|$)", text or ""):
            key = match.group(1).strip()
            value = match.group(2).strip()
            if key:
                result[key] = value
        return result

    async def _resolve_profile(
        self, session_key: str, target: str
    ) -> tuple[str, str] | None:
        """把「用户 ID 或昵称」解析成 (session_key, user_id)。

        先在当前会话里找，找不到再在所有会话里找。
        """
        target = (target or "").strip()
        if not target:
            return None
        current = await self.store.list_profiles(session_key)
        for item in current:
            if str(item.get("user_id")) == target:
                return session_key, target
        for item in current:
            if str(item.get("nickname") or "") == target:
                return session_key, str(item.get("user_id") or "")
        others = await self.store.list_profiles(None)
        for item in others:
            if str(item.get("user_id")) == target:
                return str(item.get("session_key") or ""), target
        for item in others:
            if str(item.get("nickname") or "") == target:
                return str(item.get("session_key") or ""), str(
                    item.get("user_id") or ""
                )
        return None

    @filter.permission_type(filter.PermissionType.ADMIN)
    @companion.command("好感", alias={"affection", "好感度"})
    async def companion_affection(self, event: AstrMessageEvent):
        """查看自己（或指定用户）的好感度"""
        umo = event.unified_msg_origin
        tail = self._cmd_tail(event, "好感", "affection", "好感度")
        target = tail.split(" ")[0] if tail else ""
        self_id = ""
        try:
            self_id = str(event.get_sender_id() or "")
        except Exception:
            self_id = ""
        if not target or target in ("我", "自己", "me", "本人"):
            target = self_id
        resolved = await self._resolve_profile(umo, target)
        if resolved is None:
            if target and target != self_id:
                yield event.plain_result(
                    f"没有找到「{target}」的好感记录——先和 TA 聊两句就有啦～"
                )
            else:
                yield event.plain_result("还没有你的好感记录，先聊两句吧～")
            return
        skey, uid = resolved
        profile = await self.store.get_profile(skey, uid)
        if not profile:
            yield event.plain_result("还没有好感记录，先聊两句吧～")
            return
        score = await self._affection_of(skey, uid)
        level = affection.level_of(score)
        idx = affection.level_index(score)
        bar = "█" * idx + "░" * (len(affection.LEVELS) - 1 - idx)
        extra = ""
        if uid != self_id:
            extra = f"（{profile.get('nickname') or uid}）"
        yield event.plain_result(
            f"好感度{extra}：{score:.1f} / 100\n"
            f"关系：{level}  {bar}\n"
            f"互动次数：{int(profile.get('msg_count') or 0)} 条"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @companion.command("画像", alias={"profile", "档案"})
    async def companion_profile(self, event: AstrMessageEvent):
        """查看人物画像"""
        umo = event.unified_msg_origin
        tail = self._cmd_tail(event, "画像", "profile", "档案")
        target = tail.split(" ")[0] if tail else ""
        self_id = ""
        try:
            self_id = str(event.get_sender_id() or "")
        except Exception:
            self_id = ""
        if not target or target in ("我", "自己", "me", "本人"):
            target = self_id
        resolved = await self._resolve_profile(umo, target)
        if resolved is None:
            if target and target != self_id:
                yield event.plain_result(
                    f"还没有「{target}」的画像，聊几句就会出现啦～"
                )
            else:
                yield event.plain_result("还没有你的画像，聊两句就会出现啦～")
            return
        skey, uid = resolved
        profile = await self.store.get_profile(skey, uid)
        if not profile:
            yield event.plain_result("还没有这个人的画像，聊几句就会出现啦～")
            return
        yield event.plain_result(await self._render_profile(profile))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @companion.command("改画像", alias={"setprofile", "编辑画像"})
    async def companion_edit_profile(self, event: AstrMessageEvent):
        """修改人物画像：/陪伴 改画像 <用户ID> 备注=xxx 标签=a,b 好感=60"""
        umo = event.unified_msg_origin
        tail = self._cmd_tail(event, "改画像", "setprofile", "编辑画像")
        if not tail:
            yield event.plain_result(
                "用法：/陪伴 改画像 <用户ID> 备注=这是备注 标签=朋友,技术宅 好感=60"
            )
            return
        parts = tail.split(" ", 1)
        target = parts[0].strip()
        rest = parts[1].strip() if len(parts) > 1 else ""
        self_id = ""
        try:
            self_id = str(event.get_sender_id() or "")
        except Exception:
            self_id = ""
        if target in ("我", "自己", "me", "本人"):
            target = self_id
        resolved = await self._resolve_profile(umo, target)
        if resolved is None:
            yield event.plain_result(
                f"没有找到「{target}」的画像，先聊两句试试～"
                "（也可以用昵称指定，比如：/陪伴 改画像 阿宝 备注=技术大佬）"
            )
            return
        skey, uid = resolved
        fields = self._parse_kv(rest)
        note = None
        tags = None
        affection_value = None
        nickname = None
        for key, value in fields.items():
            if key in ("备注", "note"):
                note = value
            elif key in ("标签", "tags"):
                tags = [
                    t.strip() for t in value.replace("，", ",").split(",") if t.strip()
                ]
            elif key in ("好感", "好感度", "affection"):
                try:
                    affection_value = float(value)
                except (TypeError, ValueError):
                    yield event.plain_result("好感值要是一个数字哦")
                    return
            elif key in ("昵称", "name"):
                nickname = value
        if (
            note is None
            and tags is None
            and affection_value is None
            and nickname is None
        ):
            yield event.plain_result(
                "没看懂要改什么。可用字段：备注=xxx 标签=a,b 好感=60 昵称=xxx"
            )
            return
        profile = await self.store.update_profile_fields_by_user(
            uid,
            skey,
            note=note,
            tags=tags,
            affection=affection_value,
            nickname=nickname,
        )
        if not profile:
            yield event.plain_result("没找到这个人的画像，先聊两句试试～")
            return
        if affection_value is not None:
            for row in profile:
                self._affection_cache[
                    self._affection_key(str(row.get("session_key") or ""), uid)
                ] = float(row.get("affection") or 0.0)
        extra = f"（已同步到 {len(profile)} 个会话）" if len(profile) > 1 else ""
        yield event.plain_result(
            f"已更新画像{extra}：\n" + await self._render_profile(profile[0])
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @companion.command("重建画像", alias={"rebuild", "回填画像"})
    async def companion_rebuild_profile(self, event: AstrMessageEvent):
        """用历史聊天记录补全人物画像"""
        try:
            count = await self.store.backfill_profiles()
        except Exception as exc:  # pragma: no cover
            yield event.plain_result(f"重建失败：{exc}")
            return
        await self.store.add_event("profile_rebuilt", event.unified_msg_origin)
        yield event.plain_result(
            f"已根据历史聊天记录重建 {count} 份画像（含消息数、首末出现时间等）。"
        )

    async def _render_profile(self, profile: dict[str, Any]) -> str:
        """把一条画像渲染成可读文本（含互动概况与人物速写）。"""
        session_key = str(profile.get("session_key") or "")
        user_id = str(profile.get("user_id") or "")
        score = await self._affection_of(session_key, user_id)
        level = affection.level_of(score)
        tags = _safe_json_list(profile.get("tags"))
        nickname = str(profile.get("nickname") or "") or "（未设置昵称）"
        first_seen = float(profile.get("first_seen") or 0)
        last_seen = float(profile.get("last_seen") or 0)
        msg_count = int(profile.get("msg_count") or 0)
        bot_replies = int(profile.get("bot_replies") or 0)
        fmt = "%Y-%m-%d %H:%M"
        # 互动概况：回复率能看出这个人有多爱聊
        ratio = (bot_replies / msg_count * 100) if msg_count else 0.0
        lines = [
            f"人物画像：{nickname}",
            f"用户 ID：{user_id}",
            f"好感度：{score:.1f} / 100（{level}）",
            f"互动概况：{msg_count} 条消息 · 机器人回应 {bot_replies} 次"
            f"（回应率 {ratio:.0f}%）",
            f"首次出现：{time.strftime(fmt, time.localtime(first_seen)) if first_seen else '—'}",
            f"最近出现：{time.strftime(fmt, time.localtime(last_seen)) if last_seen else '—'}",
        ]
        if tags:
            lines.append("标签：" + "、".join(str(t) for t in tags))
        note = str(profile.get("note") or "")
        lines.append("备注：" + (note if note else "（暂无）"))
        # 聊天足迹：把这个人最近说过的话也带进画像
        try:
            recent = await self.store.recent_messages_by_user(user_id, 5)
        except Exception:  # pragma: no cover
            recent = []
        if recent:
            lines.append("最近说过：")
            for item in recent:
                content = str(item.get("content") or "").replace("\n", " ").strip()
                if not content:
                    continue
                if len(content) > 40:
                    content = content[:40] + "…"
                stamp = float(item.get("ts") or 0)
                when = (
                    time.strftime("%m-%d %H:%M", time.localtime(stamp)) if stamp else ""
                )
                lines.append(f"  · [{when}] {content}")
        # 一句话人物速写
        summary = _profile_summary(
            {
                "nickname": nickname,
                "user_id": user_id,
                "level": level,
                "tags": tags,
                "note": note,
                "msg_count": msg_count,
                "bot_replies": bot_replies,
                "recent_messages": [
                    {"content": str(m.get("content") or "")} for m in recent
                ],
            }
        )
        lines.append("速写：" + summary)
        return "\n".join(lines)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @companion.command("清空", alias={"clear", "reset"})
    async def companion_clear(self, event: AstrMessageEvent):
        """清空当前会话的记忆与情绪"""
        umo = event.unified_msg_origin
        cleared = await self._clear_memory("session", session_key=umo)
        await self.store.add_event("session_cleared", umo)
        yield event.plain_result(
            f"已经忘记之前聊过的东西啦，重新认识一下吧！"
            f"（清掉 {int(cleared.get('messages') or 0)} 条聊天记录、"
            f"{int(cleared.get('profiles') or 0)} 份画像）"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @companion.command("去重", alias={"dedupe", "去重复", "清理重复"})
    async def companion_dedupe(self, event: AstrMessageEvent):
        """清理重复的聊天记录

        旧版本在平台重复投递消息时，会把同一条消息记成好几份，
        导致机器人以为「你说了好几遍」。这个指令会把重复的记录合并掉。
        """
        removed = await self.store.prune_duplicate_messages()
        await self.store.add_event("messages_deduped", event.unified_msg_origin)
        if removed:
            await self.store.backfill_profiles()
        yield event.plain_result(
            f"已清理 {removed} 条重复的聊天记录。"
            + (
                "（重复内容太多，建议过几分钟再看看机器人还会不会重复计数）"
                if removed
                else ""
            )
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @companion.command(
        "清除记忆",
        alias={"clear-memory", "forget", "忘记", "忘一忘", "清记忆", "purge"},
    )
    async def companion_forget(self, event: AstrMessageEvent):
        """一键清除记忆

        用法：
        /陪伴 清除记忆              清空当前会话（聊天记录 + 画像 + 情绪）
        /陪伴 清除记忆 阿宝          忘掉某个人（画像 + 好感度 + 他说过的话）
        /陪伴 清除记忆 全部          清空所有会话的记忆
        /陪伴 清除记忆 画像 阿宝     只清某人的画像与好感度（保留聊天记录）
        /陪伴 清除记忆 画像 全部     只清所有画像与好感度（保留聊天记录）
        """
        umo = event.unified_msg_origin
        tail = self._cmd_tail(
            event, "清除记忆", "clear-memory", "forget", "忘记", "忘一忘", "清记忆"
        )
        scope = "session"
        target = ""
        keep_messages = False
        parts = tail.split(" ") if tail else []
        parts = [p for p in parts if p]
        if parts:
            head = parts[0]
            if head in ("全部", "all", "所有"):
                scope = "all"
            elif head in ("画像", "profile", "档案"):
                scope = "profiles"
                if len(parts) > 1 and parts[1] not in ("全部", "all", "所有"):
                    target = parts[1]
            elif head in ("会话", "session", "本会话"):
                scope = "session"
            else:
                scope = "user"
                target = head
        if scope == "user":
            resolved = await self._resolve_profile(umo, target)
            if resolved is None:
                yield event.plain_result(
                    f"没有找到「{target}」的记忆，换个昵称或用户 ID 试试～"
                )
                return
            _, user_id = resolved
        elif scope == "profiles" and target:
            resolved = await self._resolve_profile(umo, target)
            if resolved is None:
                yield event.plain_result(
                    f"没有找到「{target}」的画像，换个昵称或用户 ID 试试～"
                )
                return
            _, user_id = resolved
        else:
            user_id = ""
        cleared = await self._clear_memory(
            scope,
            session_key=umo,
            user_id=user_id,
            keep_messages=keep_messages,
        )
        messages = int(cleared.get("messages") or 0)
        profiles = int(cleared.get("profiles") or 0)
        parts_out = []
        if messages:
            parts_out.append(f"{messages} 条聊天记录")
        if profiles:
            parts_out.append(f"{profiles} 份人物画像")
        if scope == "session":
            detail = "当前会话" + (
                "、" + "、".join(parts_out) if parts_out else "（本来就是空的）"
            )
            extra = "情绪与气氛也已重置。"
        elif scope == "user":
            detail = f"「{target}」的记忆" + (
                "、" + "、".join(parts_out) if parts_out else "（本来就是空的）"
            )
            extra = "好感度与画像已清空，机器人会重新认识他。"
        elif scope == "profiles":
            who = f"「{target}」的" if target else "所有人的"
            detail = (
                who
                + "画像与好感度"
                + ("、" + "、".join(parts_out) if parts_out else "（本来就是空的）")
            )
            extra = "聊天记录保留。"
        else:
            detail = "所有会话的记忆" + (
                "、" + "、".join(parts_out) if parts_out else "（本来就是空的）"
            )
            extra = "情绪与气氛已重置。"
        await self.store.add_event(
            "memory_cleared", umo, {"scope": scope, "user_id": user_id}
        )
        yield event.plain_result(f"已清除{detail}。{extra}")

    async def _clear_memory(
        self,
        scope: str,
        *,
        session_key: str = "",
        user_id: str = "",
        keep_messages: bool = False,
    ) -> dict[str, int]:
        """执行清除记忆，返回删除统计（同时清理内存里的好感度缓存）。"""
        if scope == "session":
            cleared = await self.store.clear_session_memory(
                session_key, base_arousal=emotion.BASE_AROUSAL
            )
            self._affection_cache = {
                key: value
                for key, value in self._affection_cache.items()
                if not key.startswith(f"{session_key}|")
            }
            return cleared
        if scope == "user":
            cleared = await self.store.clear_user_memory(
                user_id, keep_messages=keep_messages
            )
            self._affection_cache = {
                key: value
                for key, value in self._affection_cache.items()
                if not key.endswith(f"|{user_id}")
            }
            return cleared
        if scope == "profiles":
            if user_id:
                cleared = await self.store.clear_user_memory(
                    user_id, keep_messages=True
                )
                self._affection_cache = {
                    key: value
                    for key, value in self._affection_cache.items()
                    if not key.endswith(f"|{user_id}")
                }
                return cleared
            cleared = await self.store.clear_all_memory(
                keep_messages=True, base_arousal=emotion.BASE_AROUSAL, reset_mood=False
            )
            self._affection_cache.clear()
            return cleared
        cleared = await self.store.clear_all_memory(
            keep_messages=keep_messages, base_arousal=emotion.BASE_AROUSAL
        )
        self._affection_cache.clear()
        return cleared

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------
    def _chat_type(self, event: AstrMessageEvent) -> str:
        try:
            message_type = event.get_message_type()
            value = getattr(message_type, "value", message_type)
            text = str(value)
            if "group" in text.lower():
                return "group"
            if "friend" in text.lower() or "private" in text.lower():
                return "private"
        except Exception:
            pass
        try:
            if event.get_group_id():
                return "group"
        except Exception:
            pass
        return "private"

    def _session_title(self, event: AstrMessageEvent) -> str:
        try:
            group_id = event.get_group_id()
            if group_id:
                return f"群 {group_id}"
        except Exception:
            pass
        try:
            return event.get_sender_name() or event.get_sender_id() or ""
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Web API（供插件可视化视图调用）
    # ------------------------------------------------------------------
    def _register_web_apis(self) -> None:
        ctx = self.context
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/state", self.api_state, ["GET"], "插件运行状态"
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/sessions", self.api_sessions, ["GET"], "会话列表"
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/sessions/toggle",
            self.api_toggle_session,
            ["POST"],
            "开关会话主动消息",
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/messages/dedupe",
            self.api_dedupe_messages,
            ["POST"],
            "清理重复的聊天记录",
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/sessions/delete",
            self.api_delete_session,
            ["POST"],
            "删除会话数据",
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/sessions/reset_mood",
            self.api_reset_mood,
            ["POST"],
            "重置会话情绪",
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/sessions/send",
            self.api_send_now,
            ["POST"],
            "立即发送一条主动消息",
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/sessions/messages",
            self.api_session_messages,
            ["GET"],
            "会话消息记录",
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/config", self.api_get_config, ["GET"], "读取插件配置"
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/config/save",
            self.api_save_config,
            ["POST"],
            "保存插件配置",
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/events", self.api_events, ["GET"], "事件日志"
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/export", self.api_export, ["GET"], "导出全部数据"
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/profiles",
            self.api_profiles,
            ["GET"],
            "人物画像列表",
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/profiles/update",
            self.api_update_profile,
            ["POST"],
            "修改人物画像",
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/profiles/detail",
            self.api_profile_detail,
            ["GET"],
            "人物画像详情",
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/profiles/rebuild",
            self.api_rebuild_profiles,
            ["POST"],
            "用历史聊天记录重建人物画像",
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/memory/clear",
            self.api_clear_memory,
            ["POST"],
            "一键清除记忆（会话 / 某人 / 全部 / 只清画像）",
        )

    async def api_clear_memory(self):
        """一键清除记忆。

        scope 取值：
        - session：清空某个会话的记忆（聊天记录 + 画像 + 情绪）
        - user：忘掉某个人（画像 + 好感度 + 他说过的话）
        - profiles：只清画像与好感度，保留聊天记录
        - all：清空所有会话的记忆
        """
        payload = await request.json(default={})
        scope = str(payload.get("scope") or "session").strip()
        session_key = str(payload.get("session_key") or "").strip()
        user_id = str(payload.get("user_id") or "").strip()
        keep_messages = bool(payload.get("keep_messages"))
        if scope == "session" and not session_key:
            return error_response("缺少 session_key")
        if scope == "user" and not user_id:
            return error_response("缺少 user_id")
        # scope="profiles" 表示清空**所有**画像，本来就不需要 user_id
        if scope not in ("session", "user", "profiles", "all"):
            return error_response("scope 只能是 session / user / profiles / all")
        try:
            cleared = await self._clear_memory(
                scope,
                session_key=session_key,
                user_id=user_id,
                keep_messages=keep_messages,
            )
        except Exception as exc:  # pragma: no cover
            logger.warning(f"[主动陪伴] 清除记忆失败: {exc}")
            return error_response(f"清除失败：{exc}", status_code=500)
        await self.store.add_event(
            "memory_cleared", session_key, {"scope": scope, "user_id": user_id}
        )
        return json_response({"cleared": True, "scope": scope, "detail": cleared})

    async def api_rebuild_profiles(self):
        """用历史聊天记录回填人物画像。"""
        try:
            count = await self.store.backfill_profiles()
        except Exception as exc:  # pragma: no cover
            return error_response(f"重建失败：{exc}", status_code=500)
        await self.store.add_event("profile_rebuilt", "")
        return json_response({"rebuilt": count})

    async def api_profiles(self):
        """人物画像列表（按人合并，跨会话汇总）。"""
        session_key = (request.query.get("session_key") or "").strip() or None
        profiles = await self.store.list_profiles(session_key)
        merged: dict[str, dict[str, Any]] = {}
        for row in profiles:
            uid = str(row.get("user_id") or "")
            if not uid:
                continue
            row_session = str(row.get("session_key") or "")
            row_last = float(row.get("last_seen") or 0)
            tags = _safe_json_list(row.get("tags"))
            item = merged.get(uid)
            if item is None:
                merged[uid] = {
                    "session_key": row_session,
                    "user_id": uid,
                    "nickname": row.get("nickname") or "",
                    "affection": float(row.get("affection") or 0.0),
                    "tags": tags,
                    "note": row.get("note") or "",
                    "msg_count": int(row.get("msg_count") or 0),
                    "bot_replies": int(row.get("bot_replies") or 0),
                    "first_seen": float(row.get("first_seen") or 0),
                    "last_seen": row_last,
                    "session_count": 1,
                    "sessions": [row_session],
                    "nickname_manual": int(row.get("nickname_manual") or 0),
                }
                continue
            # 同一个人：合并各项统计，昵称/备注取最近活跃的那份
            item["session_count"] += 1
            item["sessions"].append(row_session)
            item["msg_count"] += int(row.get("msg_count") or 0)
            item["bot_replies"] += int(row.get("bot_replies") or 0)
            first_seen = float(row.get("first_seen") or 0)
            if first_seen and (
                item["first_seen"] <= 0 or first_seen < item["first_seen"]
            ):
                item["first_seen"] = first_seen
            if row_last >= item["last_seen"]:
                item["last_seen"] = row_last
                item["session_key"] = row_session
                item["affection"] = float(row.get("affection") or 0.0)
                item["nickname"] = row.get("nickname") or item["nickname"]
                item["nickname_manual"] = int(row.get("nickname_manual") or 0)
            if not item["tags"] and tags:
                item["tags"] = tags
            if not item["note"] and row.get("note"):
                item["note"] = row.get("note")
        items: list[dict[str, Any]] = []
        for item in merged.values():
            score = await self._affection_of(item["session_key"], item["user_id"])
            item["affection"] = round(score, 2)
            item["level"] = affection.level_of(score)
            item["level_index"] = affection.level_index(score)
            try:
                recent = await self.store.recent_messages_by_user(item["user_id"], 3)
            except Exception:  # pragma: no cover
                recent = []
            item["recent_messages"] = [
                {
                    "content": str(m.get("content") or ""),
                    "ts": float(m.get("ts") or 0),
                    "session_key": str(m.get("session_key") or ""),
                }
                for m in recent
                if str(m.get("content") or "").strip()
            ]
            item["summary"] = _profile_summary(item)
            items.append(item)
        items.sort(key=lambda x: float(x.get("affection") or 0), reverse=True)
        return json_response({"profiles": items, "total": len(items)})

    async def api_profile_detail(self):
        """单个人物画像详情。"""
        session_key = (request.query.get("session_key") or "").strip()
        user_id = (request.query.get("user_id") or "").strip()
        if not session_key or not user_id:
            return error_response("缺少 session_key 或 user_id")
        profile = await self.store.get_profile(session_key, user_id)
        if profile is None:
            return error_response("画像不存在", status_code=404)
        score = await self._affection_of(session_key, user_id)
        try:
            tags = json.loads(profile.get("tags") or "[]")
        except (TypeError, ValueError):
            tags = []
        profile = dict(profile)
        profile["tags"] = tags
        profile["affection"] = round(score, 2)
        profile["level"] = affection.level_of(score)
        profile["text"] = await self._render_profile(profile)
        return json_response({"profile": profile})

    async def api_update_profile(self):
        """修改人物画像：备注 / 标签 / 好感度 / 昵称。

        同一个人可能同时在群聊和私聊里跟机器人说话，之前画像按会话各存一份，
        只改其中一份的话另一份看起来就像「没生效」。现在统一按 user_id
        写入所有会话的画像，并在写完后回读校验，把真正落库的结果返回给面板。
        """
        payload = await request.json(default={})
        session_key = str(payload.get("session_key") or "").strip()
        user_id = str(payload.get("user_id") or "").strip()
        if not user_id:
            return error_response("缺少 user_id")
        note = payload.get("note")
        tags = payload.get("tags")
        nickname = payload.get("nickname")
        affection_value = payload.get("affection")
        # 昵称没改动时不要标记为「手动设置」，否则平台昵称后续就再也更新不了
        if isinstance(nickname, str) and nickname.strip():
            current = await self.store.get_profile(session_key, user_id)
            if current and str(current.get("nickname") or "") == nickname:
                nickname = None
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.replace("，", ",").split(",") if t.strip()]
        elif isinstance(tags, list):
            tags = [str(t).strip() for t in tags if str(t).strip()]
        if affection_value is not None and affection_value != "":
            try:
                affection_value = float(affection_value)
            except (TypeError, ValueError):
                return error_response("好感值必须是数字")
        else:
            affection_value = None
        try:
            profiles = await self.store.update_profile_fields_by_user(
                user_id,
                session_key,
                note=note if isinstance(note, str) else None,
                tags=tags if isinstance(tags, list) else None,
                affection=affection_value,
                nickname=nickname if isinstance(nickname, str) else None,
            )
        except Exception as exc:  # pragma: no cover
            logger.warning(f"[主动陪伴] 写入人物画像失败: {exc}")
            return error_response(f"写入失败：{exc}", status_code=500)
        if not profiles:
            return error_response("画像写入失败：没有匹配的记录", status_code=404)
        if affection_value is not None:
            for row in profiles:
                self._affection_cache[
                    self._affection_key(str(row.get("session_key") or ""), user_id)
                ] = float(row.get("affection") or 0.0)
        # 回读校验：确认要改的字段确实落库了
        primary = profiles[0]
        applied = {
            "nickname": str(primary.get("nickname") or ""),
            "tags": _safe_json_list(primary.get("tags")),
            "note": str(primary.get("note") or ""),
            "affection": round(float(primary.get("affection") or 0.0), 2),
            "sessions": len(profiles),
        }
        await self.store.add_event(
            "profile_updated",
            session_key,
            {"user_id": user_id, "sessions": len(profiles)},
        )
        return json_response({"saved": True, "profile": primary, "applied": applied})

    async def api_state(self):
        """概览信息。"""
        sessions = await self.store.list_sessions()
        now = time.time()
        today = _dt.date.today().isoformat()
        active = 0
        sent_today = 0
        reply_total = 0
        for s in sessions:
            if int(s.get("enabled") or 0):
                active += 1
            if str(s.get("daily_date") or "") == today:
                sent_today += int(s.get("daily_count") or 0)
            reply_total += int(s.get("proactive_replies") or 0)
        quiet = self._cfg_list("quiet_hours", [])
        moods = [self._session_mood(s) for s in sessions] or [emotion.EmotionState()]
        avg_valence = sum(m.valence for m in moods) / len(moods)
        avg_arousal = sum(m.arousal for m in moods) / len(moods)
        return json_response(
            {
                "plugin": PLUGIN_NAME,
                "enabled": self._cfg_bool("enabled", True),
                "running": bool(self._task and not self._task.done()),
                "interval": self._cfg_int("check_interval_seconds", 60),
                "sessions": len(sessions),
                "active_sessions": active,
                "sent_today": sent_today,
                "quiet_hours": quiet,
                "in_quiet_hours": dnd.in_quiet_hours(_dt.datetime.now(), quiet),
                "private_limits": self._effective_limits("private"),
                "group_limits": self._effective_limits("group"),
                "next_available": (
                    dnd.next_active_time(_dt.datetime.now(), quiet).isoformat()
                    if dnd.in_quiet_hours(_dt.datetime.now(), quiet)
                    else None
                ),
                "avg_valence": round(avg_valence, 3),
                "avg_arousal": round(avg_arousal, 3),
                "mood_label": emotion.label_for(
                    emotion.EmotionState(avg_valence, avg_arousal)
                ),
                "last_tick_at": self._last_tick_at,
                "last_tick_error": self._last_tick_error,
                "tts_enabled": self._cfg_bool("tts_enabled", False),
                "proactive_reply_enabled": self._cfg_bool(
                    "proactive_reply_enabled", False
                ),
                "proactive_reply_probability": round(
                    max(
                        0.0,
                        min(1.0, self._cfg_float("proactive_reply_probability", 0.2)),
                    ),
                    3,
                ),
                "proactive_reply_whitelist": self._cfg_list(
                    "proactive_reply_whitelist", []
                ),
                "proactive_replies_total": reply_total,
                "proactive_reply_last_decision": self._last_reply_decision,
                "proactive_reply_skip_reasons": dict(
                    sorted(
                        self._reply_skip_reasons.items(),
                        key=lambda kv: kv[1],
                        reverse=True,
                    )[:5]
                ),
                "capture_context_enabled": self._cfg_bool(
                    "capture_context_enabled", True
                ),
                "captured_sessions": len(self._tracked_sessions),
                "keep_messages_per_session": self._cfg_int(
                    "keep_messages_per_session", 200
                ),
                "keep_messages_days": self._cfg_int("keep_messages_days", 30),
                "atmosphere_enabled": self._cfg_bool("atmosphere_enabled", True),
                "atmosphere_levels": self._atmosphere_levels(),
                "data_dir": str(self.data_dir),
                "server_time": now,
            }
        )

    async def api_sessions(self):
        """会话列表。"""
        keyword = (request.query.get("q") or "").strip().lower()
        sessions = await self.store.list_sessions()
        result = []
        levels: dict[str, int] = {}
        for s in sessions:
            mood = self._session_mood(s)
            state = self._atmosphere_of(str(s.get("session_key") or ""))
            level = state.level if state is not None else "未知"
            levels[level] = levels.get(level, 0) + 1
            item = {
                "session_key": s.get("session_key"),
                "title": s.get("title") or s.get("session_key"),
                "platform": s.get("platform") or "",
                "chat_type": s.get("chat_type") or "",
                "enabled": bool(int(s.get("enabled") or 0)),
                "messages_sent": int(s.get("messages_sent") or 0),
                "proactive_replies": int(s.get("proactive_replies") or 0),
                "daily_count": int(s.get("daily_count") or 0),
                "last_msg_at": float(s.get("last_msg_at") or 0),
                "last_proactive_at": float(s.get("last_proactive_at") or 0),
                "valence": round(mood.valence, 3),
                "arousal": round(mood.arousal, 3),
                "mood_label": emotion.label_for(mood),
                "atmosphere": state.to_dict() if state is not None else {},
                "atmosphere_level": level,
                "mentioned": self._mentioned_recently(s, time.time()),
            }
            if keyword:
                haystack = f"{item['title']} {item['session_key']}".lower()
                if keyword not in haystack:
                    continue
            result.append(item)
        return json_response(
            {"sessions": result, "total": len(result), "atmosphere_levels": levels}
        )

    async def api_dedupe_messages(self):
        """清理重复的聊天记录（面板按钮）。"""
        removed = await self.store.prune_duplicate_messages()
        await self.store.add_event("messages_deduped", "", {"removed": removed})
        if removed:
            try:
                await self.store.backfill_profiles()
            except Exception as exc:  # pragma: no cover
                logger.warning(f"[主动陪伴] 重建画像失败: {exc}")
        return json_response({"removed": removed})

    async def api_toggle_session(self):
        """开启/关闭某个会话的主动消息。"""
        payload = await request.json(default={})
        key = str(payload.get("session_key") or "").strip()
        if not key:
            return error_response("缺少 session_key")
        session = await self.store.get_session(key)
        if not session:
            return error_response("会话不存在", status_code=404)
        enabled = payload.get("enabled")
        if enabled is None:
            enabled = not bool(int(session.get("enabled") or 0))
        await self.store.update_session(key, enabled=1 if enabled else 0)
        await self.store.add_event("session_toggled", key, {"enabled": bool(enabled)})
        return json_response({"session_key": key, "enabled": bool(enabled)})

    async def api_delete_session(self):
        """删除会话及其消息。"""
        payload = await request.json(default={})
        key = str(payload.get("session_key") or "").strip()
        if not key:
            return error_response("缺少 session_key")
        ok = await self.store.delete_session(key)
        # 内存里的缓存也要一起清掉，否则：
        # - 采集名额被永久占用，新会话再也采集不到上下文；
        # - 面板仍显示已删除会话的气氛，成了幽灵会话。
        self._tracked_sessions.discard(key)
        self._atmosphere_cache.pop(key, None)
        self._atmosphere_refreshed_at.pop(key, None)
        self._latest_names.pop(key, None)
        self._proactive_reply_times.pop(key, None)
        await self.store.add_event("session_deleted", key)
        return json_response({"deleted": ok})

    async def api_reset_mood(self):
        """重置某个会话的情绪。"""
        payload = await request.json(default={})
        key = str(payload.get("session_key") or "").strip()
        if not key:
            return error_response("缺少 session_key")
        session = await self.store.get_session(key)
        if not session:
            return error_response("会话不存在", status_code=404)
        await self.store.update_session(
            key,
            valence=0.0,
            arousal=emotion.BASE_AROUSAL,
            mood_updated_at=time.time(),
        )
        await self.store.add_event("mood_reset", key)
        return json_response({"session_key": key, "mood": "平静"})

    async def api_send_now(self):
        """立即为某个会话生成并发送一条主动消息。"""
        payload = await request.json(default={})
        key = str(payload.get("session_key") or "").strip()
        if not key:
            return error_response("缺少 session_key")
        session = await self.store.get_session(key)
        if not session:
            return error_response("会话不存在", status_code=404)
        mood = self._session_mood(session)
        text = await self._compose_message(
            session, mood, idle_minutes=0.0, now=time.time(), manual=True
        )
        if not text:
            return json_response({"sent": False, "reason": "没有可发送的内容"})
        sent = await self._deliver(key, text)
        if sent:
            await self.store.add_message(
                key, "proactive", text, "bot", "", dedupe_window=10.0
            )
            # 面板「立即发送」同样计入每日额度
            today = _dt.date.today().isoformat()
            daily_count = (
                int(session.get("daily_count") or 0)
                if str(session.get("daily_date") or "") == today
                else 0
            )
            await self.store.update_session(
                key,
                last_proactive_at=time.time(),
                messages_sent=int(session.get("messages_sent") or 0) + 1,
                daily_count=daily_count + 1,
                daily_date=today,
            )
            await self.store.add_event("web_proactive", key, {"text": text})
        return json_response({"sent": sent, "text": text})

    async def api_session_messages(self):
        """查看某个会话的近期消息。"""
        key = (request.query.get("session_key") or "").strip()
        if not key:
            return error_response("缺少 session_key")
        limit = request.query.get("limit", 50, type=int)
        messages = await self.store.recent_messages(key, max(1, min(limit, 200)))
        return json_response({"messages": messages})

    async def api_get_config(self):
        """读取当前配置（对敏感项做脱敏）。"""
        config: dict[str, Any] = {}
        for key in SAFE_DEFAULTS:
            value = self._cfg(key)
            config[key] = value
        return json_response({"config": config, "defaults": SAFE_DEFAULTS})

    async def api_save_config(self):
        """保存配置。"""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("配置格式错误")
        data = payload.get("config")
        if not isinstance(data, dict):
            data = {k: v for k, v in payload.items() if k in SAFE_DEFAULTS}
        updated = False
        schema = self._schema()
        for key, value in data.items():
            if key not in SAFE_DEFAULTS:
                continue
            value = self._coerce_value(schema.get(key), value)
            if self.config.get(key) != value:
                self.config[key] = value
                updated = True
        if updated:
            try:
                self.config.save_config()
            except Exception as exc:
                logger.warning(f"[主动陪伴] 保存配置失败: {exc}")
                return error_response(f"保存配置失败: {exc}", status_code=500)
        await self.store.add_event("config_saved", payload={"keys": list(data.keys())})
        return json_response(
            {"saved": True, "config": {k: self._cfg(k) for k in SAFE_DEFAULTS}}
        )

    async def api_events(self):
        """事件日志。"""
        limit = request.query.get("limit", 100, type=int)
        events = await self.store.recent_events(max(1, min(limit, 500)))
        return json_response({"events": events})

    async def api_export(self):
        """导出全部数据（JSON）。"""
        sessions = await self.store.list_sessions()
        data = []
        for s in sessions:
            messages = await self.store.recent_messages(str(s.get("session_key")), 100)
            item = dict(s)
            item["mood_label"] = emotion.label_for(self._session_mood(s))
            item["messages"] = messages
            data.append(item)
        return json_response({"exported_at": _now_str(), "sessions": data})
