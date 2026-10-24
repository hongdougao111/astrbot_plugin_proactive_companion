"""AstrBot 主动陪伴插件。

让 Bot 能在私聊与群聊中**主动发起消息**，并提供：

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
from astrbot.api.message_components import Record
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import error_response, json_response, request

try:
    # 正常情况：AstrBot 以 `data.plugins.<插件名>.main` 的形式加载插件，相对导入可用。
    from .core import dnd, emotion, generator
    from .core.store import Store
except ImportError:  # pragma: no cover - 兜底：插件被以独立脚本方式加载时
    import importlib
    import sys as _sys
    from pathlib import Path as _Path

    _plugin_dir = str(_Path(__file__).resolve().parent)
    if _plugin_dir not in _sys.path:
        _sys.path.insert(0, _plugin_dir)
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
}


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class ProactiveCompanionPlugin(Star):
    """主动陪伴插件主体。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config if config is not None else AstrBotConfig()
        self.data_dir: Path = StarTools.get_data_dir(PLUGIN_NAME)
        self.store = Store(self.data_dir / "proactive.db")
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._last_tick_at: float = 0.0
        self._last_tick_error: str = ""
        self._register_web_apis()

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
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - 兜底保护
                self._last_tick_error = str(exc)
                logger.error(f"[主动陪伴] 调度循环异常: {exc}")

    # ------------------------------------------------------------------
    # 消息监听（上下文感知 + 情绪输入）
    # ------------------------------------------------------------------
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

        if content:
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
        for s in sessions:
            if int(s.get("enabled") or 0):
                active += 1
            if str(s.get("daily_date") or "") == today:
                sent_today += int(s.get("daily_count") or 0)
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
                "data_dir": str(self.data_dir),
                "server_time": now,
            }
        )

    async def api_sessions(self):
        """会话列表。"""
        keyword = (request.query.get("q") or "").strip().lower()
        sessions = await self.store.list_sessions()
        result = []
        for s in sessions:
            mood = self._session_mood(s)
            item = {
                "session_key": s.get("session_key"),
                "title": s.get("title") or s.get("session_key"),
                "platform": s.get("platform") or "",
                "chat_type": s.get("chat_type") or "",
                "enabled": bool(int(s.get("enabled") or 0)),
                "messages_sent": int(s.get("messages_sent") or 0),
                "daily_count": int(s.get("daily_count") or 0),
                "last_msg_at": float(s.get("last_msg_at") or 0),
                "last_proactive_at": float(s.get("last_proactive_at") or 0),
                "valence": round(mood.valence, 3),
                "arousal": round(mood.arousal, 3),
                "mood_label": emotion.label_for(mood),
                "mentioned": self._mentioned_recently(s, time.time()),
            }
            if keyword:
                haystack = f"{item['title']} {item['session_key']}".lower()
                if keyword not in haystack:
                    continue
            result.append(item)
        return json_response({"sessions": result, "total": len(result)})

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
        for key, value in data.items():
            if key not in SAFE_DEFAULTS:
                continue
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
