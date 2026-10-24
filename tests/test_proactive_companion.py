"""主动陪伴插件的离线自检脚本。

AstrBot 运行时无法在本地完整启动，因此这里用一套轻量的 mock 替换 AstrBot 的
API（事件、Context、Provider、TTS、Web API 等），然后真实地跑一遍插件的核心逻辑：

- 消息监听 → 会话记录 → 情绪漂移
- 调度判定：免打扰时段、最小静默、冷却、每日上限、群聊仅 @ 模式
- 主动消息生成与投递（含 TTS）
- 指令（开启/关闭/状态/说说/清空）
- Web API（概览/会话/配置/日志/导出）

运行方式：

    python tests/test_proactive_companion.py
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, extra: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append(f"{name} {extra}".strip())
        print(f"  [FAIL] {name} {extra}")


# ----------------------------------------------------------------------
# Mock AstrBot
# ----------------------------------------------------------------------
class MockLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def _log(self, level: str, msg: str, *args: Any) -> None:
        try:
            text = str(msg) % args if args else str(msg)
        except Exception:
            text = str(msg)
        self.records.append((level, text))

    def info(self, msg: str, *a: Any) -> None:
        self._log("info", msg, *a)

    def debug(self, msg: str, *a: Any) -> None:
        self._log("debug", msg, *a)

    def warning(self, msg: str, *a: Any) -> None:
        self._log("warning", msg, *a)

    def error(self, msg: str, *a: Any) -> None:
        self._log("error", msg, *a)

    def exception(self, msg: str, *a: Any) -> None:
        self._log("exception", msg, *a)


class MockAstrBotConfig(dict):
    def save_config(self, replace_config: dict | None = None, **_: Any) -> None:
        pass


class MockMultiDict:
    """模拟 astrbot.api.web.PluginMultiDict。"""

    def __init__(self, pairs: dict[str, Any] | None = None) -> None:
        self._pairs = list((pairs or {}).items())

    def get(self, key: str, default: Any = None, type: Any = None) -> Any:
        for k, v in reversed(self._pairs):
            if k == key:
                if type is None:
                    return v
                try:
                    return type(v)
                except (TypeError, ValueError):
                    return default
        return default

    def getlist(self, key: str) -> list[Any]:
        return [v for k, v in self._pairs if k == key]


class MockComponent:
    def __init__(self, type_: str = "text", **kwargs: Any) -> None:
        self.type = type_
        for k, v in kwargs.items():
            setattr(self, k, v)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{self.type}>"


class MockMessageChain:
    def __init__(self) -> None:
        self.chain: list[MockComponent] = []

    def message(self, text: str):
        # 与真实实现一致：ComponentType.Plain == "Plain"
        self.chain.append(MockComponent("Plain", text=text))
        return self

    def plain(self, text: str):
        return self.message(text)


class MockRecord(MockComponent):
    def __init__(self, file: str | None = None, **kwargs: Any) -> None:
        super().__init__("Record", file=file, **kwargs)


class MockEvent:
    """模拟 AstrMessageEvent。"""

    def __init__(
        self,
        umo: str,
        text: str,
        platform: str = "aiocqhttp",
        chat_type: str = "FriendMessage",
        group_id: str = "",
        sender: str = "user-1",
        self_id: str = "bot-1",
        mentions: bool = False,
    ) -> None:
        self.unified_msg_origin = umo
        self.message_str = text
        self._platform = platform
        self._chat_type = chat_type
        self._group_id = group_id
        self._sender = sender
        self._self_id = self_id
        self.message_obj = types.SimpleNamespace(
            message=[MockComponent("At", qq=self_id)] if mentions else [],
            group_id=group_id,
        )
        self._result: Any = None
        # 与真实事件一致：waking_check 阶段会设置这两个标记
        self.is_wake = False
        self.is_at_or_wake_command = False
        self._extras: dict[str, Any] = {}

    def set_extra(self, key: str, value: Any) -> None:
        self._extras[key] = value

    def get_extra(self, key: str, default: Any = None) -> Any:
        return self._extras.get(key, default)

    def get_session_id(self) -> str:
        return self._group_id or self._sender

    def set_result(self, reply: str) -> None:
        """模拟机器人即将发送的回复。"""
        self._result = types.SimpleNamespace(
            chain=[MockComponent("Plain", text=reply)] if reply else []
        )

    def get_result(self) -> Any:
        return self._result

    def get_platform_name(self) -> str:
        return self._platform

    def get_message_type(self) -> str:
        return self._chat_type

    def get_group_id(self) -> str:
        return self._group_id

    def get_sender_id(self) -> str:
        return self._sender

    def get_sender_name(self) -> str:
        return self._sender

    def get_self_id(self) -> str:
        return self._self_id

    def is_private_chat(self) -> bool:
        return "Friend" in self._chat_type

    def plain_result(self, text: str):
        return ("plain", text)

    def chain_result(self, chain: list):
        return ("chain", chain)


class MockProvider:
    def __init__(self, reply: str = "在呢，最近怎么样？") -> None:
        self.reply = reply
        self.calls: list[dict] = []

    async def text_chat(self, **kwargs: Any):
        self.calls.append(kwargs)

        class _Resp:
            completion_text = self.reply

        return _Resp()


class MockTTSProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_audio(self, text: str) -> str:
        self.calls.append(text)
        return str(Path(tempfile.gettempdir()) / "mock_tts_output.wav")


class MockContext:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []
        self.web_apis: dict[tuple[str, str], Any] = {}
        self.provider = MockProvider()
        self.tts = MockTTSProvider()
        self.provider_error = False
        self.conversation_manager = types.SimpleNamespace(
            get_curr_conversation_id=self._get_cid,
            get_human_readable_context=self._get_history,
        )

    async def _get_cid(self, umo: str) -> str | None:
        return "conv-1"

    async def _get_history(
        self, umo: str, cid: str, page: int = 1, page_size: int = 10
    ):
        return (["User: 之前说过的话"], 1)

    def register_web_api(
        self, route: str, handler: Any, methods: list[str], desc: str
    ) -> None:
        for m in methods:
            self.web_apis[(route, m)] = handler

    async def send_message(self, session: str, chain: Any) -> bool:
        self.sent.append((session, chain))
        return True

    async def get_using_provider_async(self, umo: str | None = None):
        if self.provider_error:
            raise RuntimeError("provider unavailable")
        return self.provider

    async def get_using_tts_provider_async(self, umo: str | None = None):
        return self.tts

    def get_provider_by_id(self, provider_id: str):
        return self.tts if provider_id == "tts-1" else None

    def sent_to(self, umo: str) -> list[Any]:
        return [chain for session, chain in self.sent if session == umo]


class MockRequest:
    """模拟 astrbot.api.web.request。"""

    def __init__(self) -> None:
        self.query = MockMultiDict()
        self._json: Any = {}
        self.method = "GET"

    def set_json(self, data: Any) -> None:
        self._json = data

    def set_query(self, **kwargs: Any) -> None:
        self.query = MockMultiDict(kwargs)

    async def json(self, default: Any = None) -> Any:
        return self._json if self._json is not None else default


class MockResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code


def build_fake_astrbot(request_holder: dict[str, MockRequest]) -> None:
    """构造假的 astrbot.api 包并注册到 sys.modules。"""
    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []  # type: ignore[attr-defined]
    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = MockAstrBotConfig  # type: ignore[attr-defined]
    api.logger = MockLogger()  # type: ignore[attr-defined]

    api_event = types.ModuleType("astrbot.api.event")
    api_event.AstrMessageEvent = MockEvent  # type: ignore[attr-defined]
    api_event.MessageChain = MockMessageChain  # type: ignore[attr-defined]

    class EventMessageType:
        ALL = "ALL"
        PRIVATE_MESSAGE = "PRIVATE_MESSAGE"
        GROUP_MESSAGE = "GROUP_MESSAGE"

    class _CommandGroup:
        def __init__(self, name: str) -> None:
            self.name = name
            self.commands: dict[str, Any] = {}

        def command(self, name: str, alias: Any = None):
            def decorator(func):
                self.commands[name] = func
                return func

            return decorator

        def group(self, name: str):
            return _CommandGroup(name)

    class _Filter:
        @staticmethod
        def command(name: str, alias: Any = None):
            def decorator(func):
                func._cmd_name = name
                return func

            return decorator

        @staticmethod
        def command_group(name: str, alias: Any = None):
            def decorator(obj):
                return _CommandGroup(name)

            return decorator

        @staticmethod
        def event_message_type(kind: Any):
            def decorator(func):
                return func

            return decorator

        @staticmethod
        def on_astrbot_loaded():
            def decorator(func):
                return func

            return decorator

        @staticmethod
        def on_decorating_result():
            def decorator(func):
                return func

            return decorator

        @staticmethod
        def platform_adapter_type(kind: Any):
            def decorator(func):
                return func

            return decorator

        @staticmethod
        def permission_type(kind: Any):
            def decorator(func):
                return func

            return decorator

        @staticmethod
        def custom_filter(filter_cls: Any, *args: Any):
            def decorator(func):
                func._custom_filter = filter_cls
                return func

            return decorator

        @staticmethod
        def on_llm_request():
            def decorator(func):
                return func

            return decorator

        @staticmethod
        def on_llm_response():
            def decorator(func):
                return func

            return decorator

    class CustomFilter:
        """模拟 astrbot.api.event.filter.CustomFilter。"""

        def __init__(self, raise_error: bool = True, **kwargs: Any) -> None:
            self.raise_error = raise_error

        def filter(self, event: Any, cfg: Any) -> bool:
            return False

    # 真实的 filter 是一个模块（不是实例），插件会 `from astrbot.api.event.filter import CustomFilter`
    api_filter = types.ModuleType("astrbot.api.event.filter")
    for _fn_name in (
        "command",
        "command_group",
        "event_message_type",
        "on_astrbot_loaded",
        "on_decorating_result",
        "custom_filter",
        "on_llm_request",
        "on_llm_response",
        "platform_adapter_type",
        "permission_type",
    ):
        setattr(api_filter, _fn_name, getattr(_Filter, _fn_name))
    api_filter.CustomFilter = CustomFilter
    api_filter.EventMessageType = EventMessageType
    api_filter.PermissionType = type("PermissionType", (), {})
    api_filter.PlatformAdapterType = type("PlatformAdapterType", (), {})
    api_event.filter = api_filter  # type: ignore[assignment]
    sys.modules["astrbot.api.event.filter"] = api_filter

    api_msg = types.ModuleType("astrbot.api.message_components")
    api_msg.Record = MockRecord  # type: ignore[attr-defined]

    # astrbot.core.agent.message：插件用它注入临时的上下文内容块
    astrbot_core = types.ModuleType("astrbot.core")
    astrbot_core.__path__ = []  # type: ignore[attr-defined]
    astrbot_agent = types.ModuleType("astrbot.core.agent")
    astrbot_agent.__path__ = []  # type: ignore[attr-defined]
    astrbot_agent_msg = types.ModuleType("astrbot.core.agent.message")

    class TextPart:
        def __init__(self, text: str = "") -> None:
            self.type = "text"
            self.text = text
            self._no_save = False

        def mark_as_temp(self) -> TextPart:
            self._no_save = True
            return self

        def model_dump_for_context(self) -> dict[str, Any]:
            data = {"type": self.type, "text": self.text}
            if self._no_save:
                data["_no_save"] = True
            return data

    astrbot_agent_msg.TextPart = TextPart  # type: ignore[attr-defined]
    astrbot_agent.message = astrbot_agent_msg  # type: ignore[attr-defined]
    astrbot.core = astrbot_core  # type: ignore[attr-defined]
    sys.modules["astrbot.core"] = astrbot_core
    sys.modules["astrbot.core.agent"] = astrbot_agent
    sys.modules["astrbot.core.agent.message"] = astrbot_agent_msg

    api_star = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context: Any) -> None:
            self.context = context

        async def terminate(self) -> None:
            pass

    class Context:  # 仅用于类型标注
        pass

    class StarTools:
        _dir: Path | None = None

        @classmethod
        def get_data_dir(cls, plugin_name: str | None = None) -> Path:
            assert cls._dir is not None
            target = cls._dir / (plugin_name or "unknown")
            target.mkdir(parents=True, exist_ok=True)
            return target

    api_star.Star = Star  # type: ignore[attr-defined]
    api_star.Context = Context  # type: ignore[attr-defined]
    api_star.StarTools = StarTools  # type: ignore[attr-defined]

    api_web = types.ModuleType("astrbot.api.web")

    def json_response(data: Any = None, *, status_code: int = 200, headers=None):
        return MockResponse(data, status_code)

    def error_response(
        message: str, *, status_code: int = 400, data=None, headers=None
    ):
        return MockResponse({"status": "error", "message": message}, status_code)

    api_web.json_response = json_response  # type: ignore[attr-defined]
    api_web.error_response = error_response  # type: ignore[attr-defined]

    class _RequestProxy:
        def __getattr__(self, item: str) -> Any:
            holder = request_holder.get("current")
            if holder is None:
                raise RuntimeError("no request bound")
            return getattr(holder, item)

    api_web.request = _RequestProxy()  # type: ignore[attr-defined]

    astrbot.api = api  # type: ignore[attr-defined]
    api.event = api_event  # type: ignore[attr-defined]
    api.message_components = api_msg  # type: ignore[attr-defined]
    api.star = api_star  # type: ignore[attr-defined]
    api.web = api_web  # type: ignore[attr-defined]

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = api_event
    sys.modules["astrbot.api.message_components"] = api_msg
    sys.modules["astrbot.api.star"] = api_star
    sys.modules["astrbot.api.web"] = api_web


def load_plugin_module():
    """加载插件 main.py。"""
    import importlib

    for name in list(sys.modules):
        if name == "core" or name.startswith("core."):
            del sys.modules[name]
    if str(PLUGIN_ROOT) not in sys.path:
        sys.path.insert(0, str(PLUGIN_ROOT))
    return importlib.import_module("main")


async def reset_limits(plugin) -> None:
    """清空限流状态，方便逐项测试。"""
    today = _dt.date.today().isoformat()
    for session in await plugin.store.list_sessions():
        await plugin.store.update_session(
            str(session["session_key"]),
            daily_count=0,
            daily_date=today,
            last_proactive_at=0.0,
        )


async def mock_await_result(gen: Any) -> Any:
    """把异步生成器取第一条结果，用于测试指令。"""
    if gen is None:
        return None
    try:
        async for item in gen:
            return item
    except TypeError:
        return None
    return None


# ----------------------------------------------------------------------
# 测试主体
# ----------------------------------------------------------------------
async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="pac_test_"))
    request_holder: dict[str, MockRequest] = {}
    build_fake_astrbot(request_holder)

    main_mod = load_plugin_module()
    from astrbot.api.star import StarTools  # type: ignore

    StarTools._dir = tmp  # type: ignore[attr-defined]

    ctx = MockContext()
    config = MockAstrBotConfig(
        {
            "enabled": True,
            "check_interval_seconds": 15,
            "quiet_hours": [],
            "min_idle_minutes": 0,
            "cooldown_minutes": 0,
            "max_daily_messages": 3,
            "group_enabled": True,
            "private_enabled": True,
            "mention_only_in_group": False,
            "mood_enabled": True,
            "mood_decay_per_hour": 0.15,
            "tts_enabled": False,
            "tts_send_text": True,
            "tts_provider_id": "",
            "tts_max_chars": 50,
            "system_prompt": "你是测试用的伙伴。",
            "max_context_messages": 6,
        }
    )

    plugin = main_mod.ProactiveCompanionPlugin(ctx, config)  # type: ignore[arg-type]
    PRIV = "aiocqhttp:FriendMessage:user-1"

    print("\n== 1. 基础初始化 ==")
    check("插件实例化成功", plugin is not None)
    check("数据目录已创建", plugin.data_dir.exists())
    check("Web API 已注册", len(ctx.web_apis) >= 10, f"got={len(ctx.web_apis)}")

    print("\n== 2. 消息监听与持久化 ==")
    ev_happy = MockEvent(PRIV, "你好呀，今天很开心！")
    ev_happy.set_result("我也很开心～")
    await plugin.capture_turn(ev_happy)
    session = await plugin.store.get_session(PRIV)
    check("会话已创建", session is not None)
    check("会话类型为 private", (session or {}).get("chat_type") == "private")
    stored = await plugin.store.recent_messages(PRIV)
    check("消息已落库", len(stored) == 2, f"got={len(stored)}")
    check(
        "同时记录了用户与机器人发言",
        {m["role"] for m in stored} == {"user", "bot"},
        str([m["role"] for m in stored]),
    )
    mood = plugin._session_mood(session or {})
    check("情绪因正面词汇上升", mood.valence > 0, f"valence={mood.valence}")

    ev_angry = MockEvent(PRIV, "讨厌你，真烦")
    ev_angry.set_result("抱歉啦～")
    await plugin.capture_turn(ev_angry)
    session = await plugin.store.get_session(PRIV)
    mood2 = plugin._session_mood(session or {})
    check(
        "负面词汇拉低情绪",
        mood2.valence < mood.valence,
        f"{mood.valence} -> {mood2.valence}",
    )

    print("\n== 3. 主动消息投递 ==")
    await reset_limits(plugin)
    ctx.sent.clear()
    await plugin.tick()
    sent = ctx.sent_to(PRIV)
    check("主动消息已发送", len(sent) == 1, f"sent={len(sent)}")
    if sent:
        text = sent[0].chain[0].text
        check("消息内容非空", bool(text.strip()), f"text={text!r}")
        check("使用了 LLM 生成", len(ctx.provider.calls) >= 1)
        call = ctx.provider.calls[0]
        check("提示词包含情绪描述", "情绪" in call["prompt"])
        check("提示词包含上下文", "之前说过的话" in call["prompt"])

    print("\n== 4. 免打扰时段 ==")
    plugin.config["quiet_hours"] = ["00:00-23:59"]
    ctx.sent.clear()
    await reset_limits(plugin)
    await plugin.tick()
    check(
        "免打扰时段内不发送",
        len(ctx.sent_to(PRIV)) == 0,
        f"sent={len(ctx.sent_to(PRIV))}",
    )
    plugin.config["quiet_hours"] = ["08:00-09:00"]
    ctx.sent.clear()
    await reset_limits(plugin)
    await plugin.tick()
    check(
        "非免打扰时段正常发送",
        len(ctx.sent_to(PRIV)) == 1,
        f"sent={len(ctx.sent_to(PRIV))}",
    )
    plugin.config["quiet_hours"] = []

    print("\n== 5. 冷却与每日上限 ==")
    plugin.config["cooldown_minutes"] = 60
    ctx.sent.clear()
    await reset_limits(plugin)
    await plugin.tick()
    await plugin.tick()
    check(
        "冷却期内不重复发送",
        len(ctx.sent_to(PRIV)) == 1,
        f"sent={len(ctx.sent_to(PRIV))}",
    )
    plugin.config["cooldown_minutes"] = 0

    ctx.sent.clear()
    await reset_limits(plugin)
    for _ in range(5):
        await plugin.tick()
    check("每日上限生效", len(ctx.sent_to(PRIV)) == 3, f"sent={len(ctx.sent_to(PRIV))}")

    print("\n== 6. 指令 ==")
    grp = "aiocqhttp:GroupMessage:g1"
    res = await mock_await_result(
        plugin.companion_enable(
            MockEvent(grp, "开启", chat_type="GroupMessage", group_id="g1")
        )
    )
    check("开启指令有回复", res is not None and "已开启" in res[1])
    gs = await plugin.store.get_session(grp)
    check("会话已启用", gs is not None and int(gs["enabled"]) == 1)

    res = await mock_await_result(
        plugin.companion_disable(
            MockEvent(grp, "关闭", chat_type="GroupMessage", group_id="g1")
        )
    )
    check("关闭指令有回复", res is not None and "已关闭" in res[1])
    gs = await plugin.store.get_session(grp)
    check("会话已禁用", gs is not None and int(gs["enabled"]) == 0)

    res = await mock_await_result(plugin.companion_status(MockEvent(PRIV, "状态")))
    check("状态指令包含情绪", res is not None and "当前情绪" in res[1], str(res))
    check("状态指令包含时段状态", res is not None and "时段状态" in res[1])

    ctx.sent.clear()
    res = await mock_await_result(plugin.companion_talk(MockEvent(PRIV, "说说")))
    check(
        "说说指令触发发送",
        len(ctx.sent_to(PRIV)) == 1,
        f"sent={len(ctx.sent_to(PRIV))}",
    )
    check("说说指令有回复", res is not None)

    res = await mock_await_result(plugin.companion_clear(MockEvent(PRIV, "清空")))
    check("清空指令有回复", res is not None and "忘记" in res[1])
    s = await plugin.store.get_session(PRIV)
    check("情绪已重置", abs(float((s or {}).get("valence") or 0)) < 0.01)

    print("\n== 7. TTS 集成 ==")
    plugin.config["tts_enabled"] = True
    ctx.sent.clear()
    ctx.tts.calls.clear()
    await reset_limits(plugin)
    await plugin.tick()
    check("TTS 已生成语音", len(ctx.tts.calls) >= 1, f"calls={len(ctx.tts.calls)}")
    sent = ctx.sent_to(PRIV)
    if sent:
        types_in_chain = [c.type for c in sent[0].chain]
        check(
            "消息链包含语音",
            "Record" in types_in_chain,
            f"types={types_in_chain}",
        )
    plugin.config["tts_enabled"] = False

    print("\n== 7b. 纯语音（不附带文本）==")
    plugin.config["tts_enabled"] = True
    plugin.config["tts_send_text"] = False
    ctx.sent.clear()
    await reset_limits(plugin)
    await plugin.tick()
    sent = ctx.sent_to(PRIV)
    if sent:
        types_in_chain = [c.type for c in sent[0].chain]
        check(
            "纯语音模式下不含文本",
            "Plain" not in types_in_chain,
            f"types={types_in_chain}",
        )
    plugin.config["tts_enabled"] = False
    plugin.config["tts_send_text"] = True

    print("\n== 8. LLM 不可用时降级 ==")
    ctx.provider_error = True
    ctx.sent.clear()
    await reset_limits(plugin)
    await plugin.tick()
    sent = ctx.sent_to(PRIV)
    check("降级后仍能发送", len(sent) == 1, f"sent={len(sent)}")
    if sent:
        check("降级文案非空", bool(sent[0].chain[0].text.strip()))
    ctx.provider_error = False

    print("\n== 9. 群聊仅 @ 模式 ==")
    plugin.config["mention_only_in_group"] = True
    plugin.config["max_daily_messages"] = 50
    grp9 = "aiocqhttp:GroupMessage:grp9"
    ev_grp_plain = MockEvent(
        grp9, "有人在吗", chat_type="GroupMessage", group_id="grp9"
    )
    ev_grp_plain.set_result("我在的")
    await plugin.capture_turn(ev_grp_plain)
    await plugin.store.update_session(grp9, last_msg_at=time.time() - 3600)
    ctx.sent.clear()
    await reset_limits(plugin)
    await plugin.tick()
    check(
        "未被 @ 时不主动发言",
        len(ctx.sent_to(grp9)) == 0,
        f"sent={len(ctx.sent_to(grp9))}",
    )

    ev_grp_at = MockEvent(
        grp9, "@bot 在吗", chat_type="GroupMessage", group_id="grp9", mentions=True
    )
    ev_grp_at.set_result("在的")
    await plugin.capture_turn(ev_grp_at)
    await plugin.store.update_session(grp9, last_msg_at=time.time() - 3600)
    ctx.sent.clear()
    await reset_limits(plugin)
    await plugin.tick()
    check(
        "被 @ 之后可以主动发言",
        len(ctx.sent_to(grp9)) == 1,
        f"sent={len(ctx.sent_to(grp9))}",
    )
    plugin.config["mention_only_in_group"] = False
    plugin.config["max_daily_messages"] = 3

    print("\n== 10. 免打扰跨天与解析健壮性 ==")
    from core import dnd as dnd_mod  # type: ignore

    check(
        "跨午夜时段判定正确",
        dnd_mod.in_quiet_hours(_dt.datetime(2026, 1, 1, 23, 30), ["23:00-07:00"])
        and dnd_mod.in_quiet_hours(_dt.datetime(2026, 1, 1, 3, 30), ["23:00-07:00"])
        and not dnd_mod.in_quiet_hours(
            _dt.datetime(2026, 1, 1, 12, 0), ["23:00-07:00"]
        ),
    )
    check(
        "非法时段被忽略",
        not dnd_mod.in_quiet_hours(
            _dt.datetime(2026, 1, 1, 12, 0), ["abc", "", "9-99"]
        ),
    )
    check("空时段不打扰", not dnd_mod.in_quiet_hours(_dt.datetime.now(), []))

    print("\n== 11. Web API ==")
    req = MockRequest()
    request_holder["current"] = req

    state_resp = await plugin.api_state()
    check(
        "state 返回数据",
        isinstance(state_resp.payload, dict) and "sessions" in state_resp.payload,
    )
    check(
        "state 统计会话数",
        state_resp.payload["sessions"] >= 3,
        str(state_resp.payload.get("sessions")),
    )
    check("state 含免打扰状态", "in_quiet_hours" in state_resp.payload)

    resp = await plugin.api_sessions()
    check("sessions 列表非空", len(resp.payload["sessions"]) >= 3)

    req.set_query(q="grp9")
    resp = await plugin.api_sessions()
    check(
        "sessions 支持搜索",
        len(resp.payload["sessions"]) == 1,
        str(len(resp.payload["sessions"])),
    )
    req.set_query()

    req.set_json({"session_key": PRIV})
    resp = await plugin.api_toggle_session()
    check("toggle 返回成功", resp.payload.get("enabled") is False, str(resp.payload))
    resp = await plugin.api_toggle_session()
    check("再次 toggle 恢复", resp.payload.get("enabled") is True)

    req.set_json({"session_key": PRIV})
    resp = await plugin.api_reset_mood()
    check("reset_mood 成功", resp.payload.get("mood") == "平静")

    req.set_json({"session_key": PRIV})
    ctx.sent.clear()
    resp = await plugin.api_send_now()
    check("send_now 成功", resp.payload.get("sent") is True, str(resp.payload))

    req.set_query(session_key=PRIV, limit=10)
    resp = await plugin.api_session_messages()
    check("messages 返回记录", len(resp.payload["messages"]) >= 1)

    resp = await plugin.api_get_config()
    check(
        "config 返回配置",
        "config" in resp.payload and "quiet_hours" in resp.payload["config"],
    )

    req.set_json({"config": {"min_idle_minutes": 33, "tts_enabled": True}})
    resp = await plugin.api_save_config()
    check("config 保存成功", resp.payload.get("saved") is True)
    check("配置已生效", int(plugin.config["min_idle_minutes"]) == 33)
    plugin.config["min_idle_minutes"] = 0
    plugin.config["tts_enabled"] = False

    resp = await plugin.api_events()
    check("events 返回日志", len(resp.payload["events"]) >= 1)

    resp = await plugin.api_export()
    check("export 返回数据", len(resp.payload["sessions"]) >= 3)
    json.dumps(resp.payload)  # 必须可序列化

    print("\n== 12. 边界与健壮性 ==")
    bad_req = MockRequest()
    request_holder["current"] = bad_req
    bad_req.set_json({})
    resp = await plugin.api_toggle_session()
    check("缺少 session_key 返回错误", resp.status_code == 400, str(resp.status_code))

    bad_req.set_json({"session_key": "not-exist"})
    resp = await plugin.api_reset_mood()
    check("不存在的会话返回 404", resp.status_code == 404, str(resp.status_code))

    before = len(await plugin.store.recent_messages(PRIV))
    ev_blank = MockEvent(PRIV, "   ")
    ev_blank.set_result("")
    await plugin.capture_turn(ev_blank)
    after = len(await plugin.store.recent_messages(PRIV))
    check("空消息被忽略", before == after)

    ev_long = MockEvent(PRIV, "长" * 5000)
    ev_long.set_result("好的")
    await plugin.capture_turn(ev_long)
    check("超长消息可处理", True)

    await plugin.tick()
    check("tick 不抛异常", True)

    # 平台异常时不应崩溃
    async def boom(session: str, chain: Any) -> bool:
        raise RuntimeError("platform down")

    ctx.send_message = boom  # type: ignore[assignment]
    await plugin.tick()
    check("平台异常不导致插件崩溃", True)
    ctx.send_message = MockContext.send_message.__get__(ctx)  # type: ignore[assignment]

    print("\n== 13. 调度器生命周期 ==")
    await plugin.on_astrbot_loaded()
    check("调度任务已启动", plugin._task is not None and not plugin._task.done())
    await asyncio.sleep(0.05)
    await plugin.terminate()
    check("调度任务已停止", plugin._task is None)

    print("\n== 14. 主动回复判定 ==")
    plugin.config["proactive_reply_enabled"] = True
    plugin.config["proactive_reply_probability"] = 1.0
    plugin.config["proactive_reply_whitelist"] = ["user-1"]
    plugin.config["proactive_reply_cooldown_minutes"] = 0
    plugin.config["proactive_reply_min_length"] = 2

    cfg = MockAstrBotConfig({"wake_prefix": ["/"]})
    grp = "aiocqhttp:GroupMessage:grpX"
    ev_silent = MockEvent(
        grp, "今天天气真不错", chat_type="GroupMessage", group_id="grpX"
    )
    ev_silent.is_wake = False
    ev_silent.is_at_or_wake_command = False
    check("白名单命中且概率 1.0 → 触发", plugin.should_proactive_reply(ev_silent, cfg))
    check(
        "触发后事件被标记为可回复",
        (await plugin.proactive_reply(ev_silent)) is None
        and ev_silent.is_at_or_wake_command is True,
    )
    s = await plugin.store.get_session(grp)
    check("主动回复已计数", int((s or {}).get("proactive_replies") or 0) == 1)

    # 其他插件的监听器会把 is_wake 置 True，但不能因此放弃主动回复
    ev_wake = MockEvent(grp, "有人吗", chat_type="GroupMessage", group_id="grpX")
    ev_wake.is_wake = True
    ev_wake.is_at_or_wake_command = False
    check(
        "is_wake 被其他插件置真时仍可触发", plugin.should_proactive_reply(ev_wake, cfg)
    )

    ev_at = MockEvent(grp, "有人吗", chat_type="GroupMessage", group_id="grpX")
    ev_at.is_at_or_wake_command = True
    check("本来就会被 @ 唤醒时不触发", not plugin.should_proactive_reply(ev_at, cfg))

    ev_cmd = MockEvent(grp, "/陪伴 状态", chat_type="GroupMessage", group_id="grpX")
    check("指令消息不触发", not plugin.should_proactive_reply(ev_cmd, cfg))

    ev_short = MockEvent(grp, "嗯", chat_type="GroupMessage", group_id="grpX")
    check("过短消息不触发", not plugin.should_proactive_reply(ev_short, cfg))

    ev_other = MockEvent(
        "aiocqhttp:GroupMessage:grpY",
        "在吗",
        chat_type="GroupMessage",
        group_id="grpY",
        sender="user-999",
    )
    check("非白名单用户不触发", not plugin.should_proactive_reply(ev_other, cfg))

    plugin.config["proactive_reply_whitelist"] = []
    check("白名单为空 → 不限制（可触发）", plugin.should_proactive_reply(ev_other, cfg))

    plugin.config["proactive_reply_whitelist"] = ["*"]
    plugin.config["proactive_reply_probability"] = 0.0
    check("概率为 0 → 不触发", not plugin.should_proactive_reply(ev_other, cfg))

    plugin.config["proactive_reply_probability"] = 0.5
    hits = sum(1 for _ in range(200) if plugin.should_proactive_reply(ev_other, cfg))
    check("概率 0.5 大致命中一半", 40 < hits < 160, f"hits={hits}")

    plugin.config["proactive_reply_probability"] = 1.0
    plugin.config["proactive_reply_cooldown_minutes"] = 30
    ev_cd = MockEvent(
        "aiocqhttp:GroupMessage:grpZ",
        "在吗",
        chat_type="GroupMessage",
        group_id="grpZ",
    )
    check("冷却前首次可触发", plugin.should_proactive_reply(ev_cd, cfg))
    plugin._proactive_reply_times[ev_cd.unified_msg_origin] = time.time()
    check("冷却期内不再触发", not plugin.should_proactive_reply(ev_cd, cfg))

    plugin.config["quiet_hours"] = ["00:00-23:59"]
    plugin.config["proactive_reply_cooldown_minutes"] = 0
    check("免打扰时段内不触发", not plugin.should_proactive_reply(ev_other, cfg))
    plugin.config["quiet_hours"] = []

    plugin.config["proactive_reply_enabled"] = False
    check("总开关关闭时不触发", not plugin.should_proactive_reply(ev_other, cfg))
    plugin.config["proactive_reply_enabled"] = True

    plugin.config["enabled"] = False
    check("插件总开关关闭时不触发", not plugin.should_proactive_reply(ev_other, cfg))
    plugin.config["enabled"] = True

    print("\n== 15. 上下文采集与注入 ==")
    plugin.config["capture_context_enabled"] = True
    plugin.config["proactive_reply_whitelist"] = ["*"]
    ctx_ev = MockEvent(
        "aiocqhttp:GroupMessage:grpC",
        "今天天气真不错",
        chat_type="GroupMessage",
        group_id="grpC",
        sender="user-c",
    )
    plugin.capture_message_background(ctx_ev)
    await asyncio.sleep(0.3)
    stored = await plugin.store.recent_messages("aiocqhttp:GroupMessage:grpC")
    check("被动采集已记录消息", len(stored) >= 1, f"got={len(stored)}")
    check("采集后标记了事件", bool(ctx_ev.get_extra("_pac_message_captured", False)))
    check(
        "会话已进入跟踪列表",
        "aiocqhttp:GroupMessage:grpC" in plugin._tracked_sessions,
    )

    class _Req:
        def __init__(self) -> None:
            self.extra_user_content_parts: list[Any] = []

    req = _Req()
    ev_inject = MockEvent(
        "aiocqhttp:GroupMessage:grpC",
        "那我们明天下午去?",
        chat_type="GroupMessage",
        group_id="grpC",
    )
    ev_inject.set_extra("_pac_proactive_session", "aiocqhttp:GroupMessage:grpC")
    await plugin.inject_proactive_context(ev_inject, req)
    check("上下文已注入 LLM 请求", len(req.extra_user_content_parts) == 1)
    if req.extra_user_content_parts:
        part = req.extra_user_content_parts[0]
        check(
            "注入内容包含对话记录",
            "今天天气真不错" in getattr(part, "text", ""),
            getattr(part, "text", "")[:120],
        )
        check(
            "注入内容标记为临时（不写入历史）",
            getattr(part, "_no_save", False) is True,
        )

    req2 = _Req()
    ev_plain = MockEvent("aiocqhttp:GroupMessage:grpC", "普通消息")
    await plugin.inject_proactive_context(ev_plain, req2)
    check("普通请求不注入上下文", len(req2.extra_user_content_parts) == 0)

    # 采集关闭后不再记录
    plugin.config["capture_context_enabled"] = False
    before = len(await plugin.store.recent_messages("aiocqhttp:GroupMessage:grpC"))
    plugin.capture_message_background(
        MockEvent(
            "aiocqhttp:GroupMessage:grpD",
            "另一条消息",
            chat_type="GroupMessage",
            group_id="grpD",
        )
    )
    await asyncio.sleep(0.2)
    after = len(await plugin.store.recent_messages("aiocqhttp:GroupMessage:grpD"))
    check("关闭采集后不再记录", after == 0, f"got={after}")
    plugin.config["capture_context_enabled"] = True

    print("\n== 17. 读空气（气氛感知）==")
    from core import atmosphere as atmo  # type: ignore

    now = time.time()

    def _msg(content: str, sender: str, offset: float = 0.0) -> dict:
        return {
            "content": content,
            "sender": sender,
            "ts": now - offset,
            "role": "user",
        }

    # 冷清：两条消息隔了近半小时
    cold = atmo.analyze(
        [_msg("在吗", "u1", 1750), _msg("有人吗", "u2", 60)],
        now=now,
        window_minutes=30,
    )
    check("久无发言 → 冷清", cold.level == atmo.LEVEL_COLD, cold.level)
    check("冷清时给出暖场建议", "暖场" in cold.advice or "话题" in cold.advice)

    # 热闹：多人高频发言
    hot_msgs = [_msg(f"消息{i}", f"u{i % 4}", i * 5) for i in range(24)]
    hot = atmo.analyze(hot_msgs, now=now, window_minutes=30)
    check("高频多人发言 → 热闹", hot.level == atmo.LEVEL_HOT, hot.level)
    check("热闹时不插嘴", atmo.probability_factor(hot) < 1.0)

    # 紧张：出现冲突措辞
    tense_msgs = [
        _msg("你给我闭嘴", "u1", 30),
        _msg("滚蛋", "u2", 20),
        _msg("举报你", "u3", 10),
    ]
    tense = atmo.analyze(tense_msgs, now=now, window_minutes=30)
    check("冲突措辞 → 紧张", tense.level == atmo.LEVEL_TENSE, tense.level)
    check("紧张时完全不出声", atmo.probability_factor(tense) == 0.0)
    check("紧张时不发主动消息", not atmo.allow_proactive_message(tense))
    check("冷清时允许主动消息", atmo.allow_proactive_message(cold))

    # 正常
    normal = atmo.analyze(
        [_msg("今天吃了吗", "u1", 120), _msg("吃了", "u2", 60)], now=now
    )
    check("普通闲聊 → 正常", normal.level == atmo.LEVEL_NORMAL, normal.level)

    check(
        "机器人自己的发言不计入气氛",
        atmo.analyze(
            [
                {"content": "bot 刷屏", "sender": "bot", "ts": now - 10, "role": "bot"},
                {
                    "content": "bot 又刷屏",
                    "sender": "bot",
                    "ts": now - 5,
                    "role": "bot",
                },
            ],
            now=now,
        ).level
        == atmo.LEVEL_COLD,
    )

    # 插件侧：气氛会影响主动回复判定
    plugin.config["atmosphere_enabled"] = True
    plugin.config["atmosphere_boost"] = 1.5
    air_umo = "aiocqhttp:GroupMessage:air"
    plugin._atmosphere_cache[air_umo] = atmo.analyze(tense_msgs, now=now)
    ev_air = MockEvent(air_umo, "随便说句", chat_type="GroupMessage", group_id="air")
    ev_air.is_at_or_wake_command = False
    check(
        "气氛紧张 → 不触发主动回复",
        not plugin.should_proactive_reply(ev_air, cfg),
    )
    plugin._atmosphere_cache[air_umo] = atmo.analyze([_msg("在吗", "u1", 900)], now=now)
    check(
        "气氛冷清 → 仍可触发（概率被放大）",
        plugin.should_proactive_reply(ev_air, cfg),
    )
    plugin._atmosphere_cache.pop(air_umo, None)
    plugin.config["atmosphere_enabled"] = False
    check(
        "关闭读空气后不再受气氛影响",
        plugin.should_proactive_reply(ev_air, cfg),
    )
    plugin.config["atmosphere_enabled"] = True

    # 面板接口应带上气氛
    resp = await plugin.api_sessions()
    check("会话接口返回气氛字段", "atmosphere_level" in resp.payload["sessions"][0])
    resp = await plugin.api_state()
    check("概览接口返回气氛统计", "atmosphere_levels" in resp.payload)

    print("\n== 18. 配置类型自愈 ==")
    # 模拟历史遗留：list 字段被写成字符串
    broken = MockAstrBotConfig(
        {
            "proactive_reply_whitelist": "96460862,12345",
            "quiet_hours": "23:00-07:30",
            "proactive_reply_probability": 1,
        }
    )
    fixed = main_mod.ProactiveCompanionPlugin(ctx, broken)
    wl = fixed.config.get("proactive_reply_whitelist")
    check(
        "字符串白名单被自愈为 list",
        isinstance(wl, list) and wl == ["96460862", "12345"],
        repr(wl),
    )
    check(
        "字符串免打扰时段被自愈为 list",
        isinstance(fixed.config.get("quiet_hours"), list),
        repr(fixed.config.get("quiet_hours")),
    )
    check(
        "int 概率被规整为 float",
        isinstance(fixed.config.get("proactive_reply_probability"), float),
        repr(fixed.config.get("proactive_reply_probability")),
    )

    # 保存接口也要按 schema 规整类型
    req_save = MockRequest()
    request_holder["current"] = req_save
    req_save.set_json(
        {
            "config": {
                "proactive_reply_whitelist": "aaa, bbb ,ccc",
                "min_idle_minutes": 30,
            }
        }
    )
    resp = await fixed.api_save_config()
    check("保存接口返回成功", resp.payload.get("saved") is True)
    check(
        "保存后白名单是 list",
        isinstance(fixed.config.get("proactive_reply_whitelist"), list)
        and fixed.config.get("proactive_reply_whitelist") == ["aaa", "bbb", "ccc"],
        repr(fixed.config.get("proactive_reply_whitelist")),
    )

    print("\n== 20. @别人不插嘴 / 用户身份识别 ==")
    # 消息里 @ 的是别人 → 不应主动回复
    ev_at_other = MockEvent(
        "aiocqhttp:GroupMessage:grpAt",
        "你今天有空吗",
        chat_type="GroupMessage",
        group_id="grpAt",
    )
    ev_at_other.message_obj.message = [MockComponent("At", qq="someone-else")]
    check("消息在 @ 别人 → 不触发", not plugin.should_proactive_reply(ev_at_other, cfg))
    check(
        "判定原因已记录",
        "@" in plugin._last_reply_decision and "其他" in plugin._last_reply_decision,
        plugin._last_reply_decision,
    )

    # @ 机器人 → 框架本身会唤醒，不需要我们插手
    ev_at_bot = MockEvent(
        "aiocqhttp:GroupMessage:grpAt2",
        "你今天有空吗",
        chat_type="GroupMessage",
        group_id="grpAt2",
    )
    ev_at_bot.message_obj.message = [MockComponent("At", qq="bot-1")]
    ev_at_bot.is_at_or_wake_command = True
    check(
        "@ 机器人 → 不触发（框架已唤醒）",
        not plugin.should_proactive_reply(ev_at_bot, cfg),
    )

    # @ 全体成员 → 也交给框架处理
    ev_at_all = MockEvent(
        "aiocqhttp:GroupMessage:grpAt3",
        "大家看一下",
        chat_type="GroupMessage",
        group_id="grpAt3",
    )
    ev_at_all.message_obj.message = [MockComponent("At", qq="all")]
    ev_at_all.is_at_or_wake_command = True
    check("@ 全体成员 → 不触发", not plugin.should_proactive_reply(ev_at_all, cfg))

    # 用户身份：昵称 + ID 都要记录
    id_umo = "aiocqhttp:GroupMessage:grpId"
    ev_id = MockEvent(
        id_umo,
        "我叫小明",
        chat_type="GroupMessage",
        group_id="grpId",
        sender="小明",
    )
    ev_id.get_sender_id = lambda: "10001"  # type: ignore[method-assign]
    ev_id.get_sender_name = lambda: "小明"  # type: ignore[method-assign]
    plugin.capture_message_background(ev_id)
    await asyncio.sleep(0.3)
    msgs = await plugin.store.recent_messages(id_umo)
    check("消息记录了昵称", msgs and msgs[0].get("sender") == "小明", str(msgs[:1]))
    check(
        "消息记录了用户 ID", msgs and msgs[0].get("sender_id") == "10001", str(msgs[:1])
    )

    # 同名不同 ID → 上下文里要能区分
    dup_umo = "aiocqhttp:GroupMessage:grpDup"
    for sid in ("20001", "20002"):
        await plugin.store.add_message(dup_umo, "user", f"来自{sid}的话", "小明", sid)
    block = plugin._format_context_block(
        await plugin.store.recent_messages(dup_umo, 10)
    )
    check(
        "同名用户被标注 ID 以便区分",
        "小明#0001" in block and "小明#0002" in block,
        block[:200],
    )

    # 昵称不重复时保持干净
    clean_umo = "aiocqhttp:GroupMessage:grpClean"
    await plugin.store.add_message(clean_umo, "user", "你好", "小红", "30001")
    block2 = plugin._format_context_block(
        await plugin.store.recent_messages(clean_umo, 10)
    )
    check(
        "无重名时不加 ID 后缀",
        "小红:" in block2 and "小红#" not in block2,
        block2[:120],
    )

    print("\n== 21. 数据库迁移 ==")
    import sqlite3 as _sqlite3

    db_path = plugin.store.db_path
    conn = _sqlite3.connect(str(db_path))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    conn.close()
    check("sessions 表含 proactive_replies 列", "proactive_replies" in cols)
    check("sessions 表含 atmosphere_level 列", "atmosphere_level" in cols)
    check("sessions 表含 atmosphere_data 列", "atmosphere_data" in cols)

    shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 52)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("\n失败明细：")
        for f in FAILED:
            print(f"  - {f}")
        sys.exit(1)
    print("全部通过 ✅")


if __name__ == "__main__":
    asyncio.run(main())
