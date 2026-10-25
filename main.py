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
    from .core import atmosphere, dnd, emotion, generator
    from .core.store import Store
except ImportError:  # pragma: no cover - 兜底：插件被以独立脚本方式加载时
    import importlib
    import sys as _sys
    from pathlib import Path as _Path

    _plugin_dir = str(_Path(__file__).resolve().parent)
    if _plugin_dir not in _sys.path:
        _sys.path.insert(0, _plugin_dir)
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
    "mood_enabled": True,
    "mood_decay_per_hour": 0.15,
    "mood_affect_frequency": True,
    "tts_enabled": False,
    "tts_provider_id": "",
    "tts_send_text": True,
    "tts_max_chars": 120,
    "system_prompt": generator.DEFAULT_SYSTEM_PROMPT,
    "max_context_messages": 12,
    "keep_messages_per_session": 200,
    "keep_events": 1000,
    "proactive_reply_enabled": True,
    "proactive_reply_probability": 0.2,
    "proactive_reply_whitelist": [],
    "proactive_reply_cooldown_minutes": 10,
    "proactive_reply_min_length": 2,
    "capture_context_enabled": True,
    "proactive_reply_context_messages": 12,
    "capture_context_max_sessions": 200,
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
        # 被动采集：已经被记录过对话的会话
        self._tracked_sessions: set[str] = set()
        # 最近一次主动回复判定结果，便于在面板上排查为什么没触发
        self._last_reply_decision: str = "尚未判定"
        # 各判定原因的跳过次数（面板上展示）
        self._reply_skip_reasons: dict[str, int] = {}
        # 气氛缓存：session_key -> Atmosphere（供同步过滤器快速读取）
        self._atmosphere_cache: dict[str, Any] = {}
        self._atmosphere_refreshed_at: dict[str, float] = {}
        global _PLUGIN_REF
        _PLUGIN_REF = self
        self._register_web_apis()

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
        """
        umo = event.get_extra(_PROACTIVE_SESSION_KEY)
        if not umo or TextPart is None:
            return
        try:
            limit = max(1, self._cfg_int("proactive_reply_context_messages", 12))
            messages = await self.store.recent_messages(umo, limit)
            hint = self._atmosphere_hint(umo)
            if not messages and not hint:
                return
            text = self._format_context_block(messages, hint)
            if not text:
                return
            req.extra_user_content_parts.append(TextPart(text=text).mark_as_temp())
        except Exception as exc:  # pragma: no cover
            logger.warning(f"[主动陪伴] 注入上下文失败: {exc}")

    def _format_context_block(
        self, messages: list[dict[str, Any]], atmosphere_hint: str = ""
    ) -> str:
        """把记录的消息格式化成给 LLM 的上下文块。"""
        lines: list[str] = []
        for msg in messages:
            content = str(msg.get("content") or "").strip()
            if not content:
                continue
            role = str(msg.get("role") or "")
            sender = str(msg.get("sender") or "").strip()
            if role == "user":
                lines.append(f"{sender or '对方'}: {content}")
            elif role == "bot":
                lines.append(f"我: {content}")
            elif role == "proactive":
                lines.append(f"我(之前主动搭话): {content}")
            else:
                lines.append(f"{sender or '对方'}: {content}")
        if not lines:
            return atmosphere_hint
        body = "\n".join(lines)
        hint_block = f"{atmosphere_hint}\n" if atmosphere_hint else ""
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
        try:
            sender = event.get_sender_name() or event.get_sender_id() or ""
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
        await self.store.add_message(umo, "user", content, sender)
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
        if umo in self._tracked_sessions:
            return True
        whitelist = [
            str(item).strip()
            for item in self._cfg_list("proactive_reply_whitelist", [])
            if str(item).strip()
        ]
        if not whitelist or "*" in whitelist:
            # 允许所有人主动回复 → 记录所有会话的上下文
            return self._can_track_more()
        candidates: set[str] = set()
        for getter in ("get_sender_id", "get_group_id", "get_session_id"):
            try:
                candidates.add(str(getattr(event, getter)() or ""))
            except Exception:
                pass
        try:
            candidates.add(str(event.unified_msg_origin or ""))
        except Exception:
            pass
        if any(item in candidates for item in whitelist):
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
            await self.store.add_message(
                umo,
                "user",
                (event.message_str or "").strip(),
                event.get_sender_name() or "",
            )
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
        if self._looks_like_command(content, cfg):
            return False, "指令消息"
        if dnd.in_quiet_hours(_dt.datetime.now(), self._cfg_list("quiet_hours", [])):
            return False, "处于免打扰时段"
        if not self._in_reply_whitelist(event):
            return False, "不在主动回复白名单内"
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
            probability = min(1.0, probability * factor)
        if probability < 1.0 and random.random() >= probability:
            return False, f"未命中概率（{probability:.2f}）"
        return True, "命中"

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

    def _in_reply_whitelist(self, event: AstrMessageEvent) -> bool:
        """白名单判定：命中发送者 ID / 会话 ID / 群号 / umo 之一即可。

        白名单为空表示不做额外限制（谁都可以触发主动回复）；
        填 `*` 语义相同。填写具体值后，只有命中者才会触发。
        """
        whitelist = [
            str(item).strip()
            for item in self._cfg_list("proactive_reply_whitelist", [])
            if str(item).strip()
        ]
        if not whitelist or "*" in whitelist:
            return True
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
        return any(item in candidates for item in whitelist)

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
        chat_type = self._chat_type(event)
        title = self._session_title(event)
        platform = event.get_platform_name() or ""
        await self.store.ensure_session(umo, umo, platform, chat_type, title)

        sender_name = event.get_sender_name() or event.get_sender_id() or ""
        now = time.time()
        update: dict[str, Any] = {"last_msg_at": now}
        if self._mentions_bot(event):
            note = self._load_note(await self.store.get_session(umo))
            note["last_mention_at"] = now
            update["note"] = json.dumps(note, ensure_ascii=False)

        if content and not event.get_extra(_CAPTURED_FLAG, False):
            # 被动采集已经记录过这条消息时不要重复入库
            await self.store.add_message(umo, "user", content, sender_name)
        if reply:
            await self.store.add_message(umo, "bot", reply, "bot")
        await self.store.update_session(umo, **update)

        if content and self._cfg_bool("mood_enabled", True):
            await self._apply_mood_event(umo, content)

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
        # 打乱顺序，避免每次都从同一个会话开始，造成规律性打扰
        random.shuffle(sessions)
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
        if chat_type == "group" and not self._cfg_bool("group_enabled", True):
            return False
        if chat_type == "private" and not self._cfg_bool("private_enabled", True):
            return False

        quiet = self._cfg_list("quiet_hours", [])
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
        max_daily = self._cfg_int("max_daily_messages", 6)
        if max_daily > 0 and daily_count >= max_daily:
            return False

        last_msg_at = float(session.get("last_msg_at") or 0.0)
        last_proactive_at = float(session.get("last_proactive_at") or 0.0)
        anchor = max(last_msg_at, last_proactive_at)

        min_idle = self._idle_threshold_minutes()
        idle_minutes = (now - anchor) / 60.0 if anchor else 0.0
        # 读空气：群正热闹或气氛紧张时不要硬插话
        state = self._atmosphere_of(session_key)
        if state is not None:
            if not atmosphere.allow_proactive_message(state):
                return False
            if state.level == atmosphere.LEVEL_COLD:
                # 冷场时允许更早一点暖场
                min_idle = min_idle * 0.5
        if idle_minutes < min_idle:
            return False

        cooldown = self._cfg_int("cooldown_minutes", 180)
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

    def _idle_threshold_minutes(self) -> float:
        """静默阈值，带随机抖动，避免所有会话在同一时刻一起触发。"""
        base = float(self._cfg_int("min_idle_minutes", 45))
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
        history = await self._astro_history(umo)
        context_block = generator.build_context_block(
            recent,
            history,
            max_items=self._cfg_int("max_context_messages", 12),
        )
        prompt = generator.build_prompt(
            context_block,
            mood,
            idle_minutes,
            is_group=chat_type == "group",
            group_title=str(session.get("title") or ""),
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

    @companion.command("关闭", alias={"off", "disable", "stop"})
    async def companion_disable(self, event: AstrMessageEvent):
        """为当前会话关闭主动消息"""
        umo = event.unified_msg_origin
        await self.store.update_session(umo, enabled=0)
        await self.store.add_event("session_disabled", umo)
        yield event.plain_result("已关闭主动陪伴，我会安静待着的。")

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
        quiet = self._cfg_list("quiet_hours", [])
        dnd_state = (
            "免打扰中" if dnd.in_quiet_hours(_dt.datetime.now(), quiet) else "可打扰"
        )
        text = (
            f"主动陪伴状态：{'开启' if int(session.get('enabled') or 0) else '关闭'}\n"
            f"当前情绪：{emotion.label_for(mood)}"
            f"（{mood.valence:+.2f} / {mood.arousal:.2f}）\n"
            f"已发送：{int(session.get('messages_sent') or 0)} 条，"
            f"今日：{int(session.get('daily_count') or 0)}/{self._cfg_int('max_daily_messages', 6)}\n"
            f"上次主动：{last_text}\n"
            f"时段状态：{dnd_state}"
        )
        yield event.plain_result(text)

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
            await self.store.add_message(umo, "proactive", text, "bot")
            await self.store.update_session(
                umo,
                last_proactive_at=time.time(),
                messages_sent=int(session.get("messages_sent") or 0) + 1,
            )
            await self.store.add_event("manual_proactive", umo, {"text": text})
            yield event.plain_result("（已发送）")
        else:
            yield event.plain_result("消息发送失败了，请检查平台是否支持主动消息。")

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
        yield event.plain_result("\n".join(lines))

    @companion.command("清空", alias={"clear", "reset"})
    async def companion_clear(self, event: AstrMessageEvent):
        """清空当前会话的记忆与情绪"""
        umo = event.unified_msg_origin
        await self.store.update_session(
            umo,
            valence=0.0,
            arousal=emotion.BASE_AROUSAL,
            mood_updated_at=time.time(),
            last_msg_at=0.0,
        )
        await self.store.add_event("session_cleared", umo)
        yield event.plain_result("已经忘记之前聊过的东西啦，重新认识一下吧！")

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
            await self.store.add_message(key, "proactive", text, "bot")
            await self.store.update_session(
                key,
                last_proactive_at=time.time(),
                messages_sent=int(session.get("messages_sent") or 0) + 1,
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
