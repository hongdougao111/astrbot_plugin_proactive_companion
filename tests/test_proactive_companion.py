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
        message_id: str = "",
    ) -> None:
        self.unified_msg_origin = umo
        self.message_str = text
        self._platform = platform
        self._chat_type = chat_type
        self._group_id = group_id
        self._sender = sender
        self._self_id = self_id
        self._message_id = message_id
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

    def get_message_id(self) -> str:
        return self._message_id

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
                func._permission_type = kind
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
    api_filter.PermissionType = type(
        "PermissionType",
        (),
        {"ADMIN": "admin", "MEMBER": "member", "GROUP_ADMIN": "group_admin"},
    )
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
            # 测试里让私聊沿用全局阈值（0 = 跟随全局），单独策略另有专项用例
            "private_min_idle_minutes": 0,
            "private_cooldown_minutes": 0,
            "private_max_daily_messages": 0,
            "private_quiet_hours": [],
            "private_skip_atmosphere": True,
            "private_priority": True,
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

    print("\n== 21. 好感度与人物画像 ==")
    from core import affection as aff  # type: ignore

    # 等级判定
    check("0 分 → 陌生", aff.level_of(0) == "陌生")
    check("35 分 → 熟人", aff.level_of(35) == "熟人")
    check("90 分 → 挚友", aff.level_of(90) == "挚友")
    check("好感分被限制在 0~100", aff.clamp(200) == 100.0 and aff.clamp(-5) == 0.0)

    # 事件加减分
    s0 = aff.apply_event(0.0, "user_message")
    check("用户发言会加分", s0 > 0)
    s1 = aff.apply_event(s0, "bot_reply")
    check("机器人回复会加分", s1 > s0)
    s2 = aff.apply_event(s1, "proactive_reply")
    check("主动搭话加分更多", s2 > s1)
    s3 = aff.apply_event(s1, "user_message", negative=True)
    check("负面情绪会扣分", s3 < s1)

    # 衰减
    import time as _time

    decayed = aff.decayed(50.0, _time.time() - 86400 * 3)
    check("长时间不互动会衰减", decayed < 50.0, f"{decayed}")

    # 概率系数
    check("好感越高越愿意搭话", aff.probability_factor(100) > aff.probability_factor(0))

    # 落库与画像
    aff_umo = "aiocqhttp:FriendMessage:aff1"
    await plugin.store.touch_profile(
        aff_umo, "u-aff", nickname="阿宝", event="user_message", positive=True
    )
    await plugin.store.touch_profile(
        aff_umo, "u-aff", nickname="阿宝", event="bot_reply"
    )
    profile = await plugin.store.get_profile(aff_umo, "u-aff")
    check("画像已落库", profile is not None)
    check("画像记录了昵称", (profile or {}).get("nickname") == "阿宝")
    check(
        "好感度已累加",
        float((profile or {}).get("affection") or 0) > 0,
        str((profile or {}).get("affection")),
    )
    check(
        "消息计数正确",
        int((profile or {}).get("msg_count") or 0) == 1,
        str((profile or {}).get("msg_count")),
    )
    check(
        "机器人回应计数正确",
        int((profile or {}).get("bot_replies") or 0) == 1,
        str((profile or {}).get("bot_replies")),
    )

    # 修改画像
    updated = await plugin.store.update_profile_fields(
        aff_umo, "u-aff", note="技术大佬", tags=["朋友", "技术"], affection=66.0
    )
    check("备注已保存", (updated or {}).get("note") == "技术大佬")
    check(
        "标签已保存",
        "技术" in str((updated or {}).get("tags") or ""),
        str((updated or {}).get("tags")),
    )
    check("好感度可手动设置", float((updated or {}).get("affection") or 0) == 66.0)

    # 画像渲染
    text = await plugin._render_profile(updated or {})
    check("画像含昵称", "阿宝" in text)
    check("画像含好感等级", "亲近" in text, text[:200])
    check("画像含备注", "技术大佬" in text)

    # Web API
    resp = await plugin.api_profiles()
    check("画像列表接口可用", resp.payload.get("total", 0) >= 1)
    resp = await plugin.api_update_profile()
    check("画像修改缺参数时报错", resp.status_code == 400)

    print("\n== 22. 数据库迁移 ==")
    import sqlite3 as _sqlite3

    db_path = plugin.store.db_path
    conn = _sqlite3.connect(str(db_path))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    conn.close()
    check("sessions 表含 proactive_replies 列", "proactive_replies" in cols)
    check("sessions 表含 atmosphere_level 列", "atmosphere_level" in cols)
    check("sessions 表含 atmosphere_data 列", "atmosphere_data" in cols)

    conn = _sqlite3.connect(str(db_path))
    prof_cols = {row[1] for row in conn.execute("PRAGMA table_info(user_profiles)")}
    conn.close()
    check("user_profiles 表含 nickname_manual 列", "nickname_manual" in prof_cols)

    print("\n== 23. v1.0.7 修复：指令解析 / 画像生效 / 历史回填 ==")

    # 23.1 指令参数解析（AstrBot 不会剥离子指令名）
    ev = MockEvent(aff_umo, "陪伴 好感 u-aff")
    tail = plugin._cmd_tail(ev, "好感", "affection", "好感度")
    check("指令参数能剥离子指令名", tail == "u-aff", repr(tail))

    ev = MockEvent(aff_umo, "陪伴 改画像 u-aff 备注=技术大佬 标签=朋友,技术 好感=60")
    tail = plugin._cmd_tail(ev, "改画像", "setprofile", "编辑画像")
    kv = plugin._parse_kv(tail.split(" ", 1)[1] if " " in tail else "")
    check(
        "改画像参数解析正确",
        kv.get("备注") == "技术大佬" and kv.get("标签") == "朋友,技术",
        str(kv),
    )
    check("改画像能取到好感值", kv.get("好感") == "60", str(kv))

    # 23.2 指令真的能跑通（原来取错 token 导致「没反应」）
    res = await mock_await_result(
        plugin.companion_affection(MockEvent(aff_umo, "陪伴 好感 u-aff"))
    )
    check(
        "好感指令能返回目标用户的好感",
        res is not None and "好感度" in res[1] and "阿宝" in res[1],
        str(res),
    )

    res = await mock_await_result(
        plugin.companion_profile(MockEvent(aff_umo, "陪伴 画像 u-aff"))
    )
    check(
        "画像指令能返回目标用户的画像", res is not None and "阿宝" in res[1], str(res)
    )

    res = await mock_await_result(
        plugin.companion_edit_profile(
            MockEvent(aff_umo, "陪伴 改画像 u-aff 备注=老朋友 好感=42")
        )
    )
    check("改画像指令有回复", res is not None and "已更新画像" in res[1], str(res))
    prof2 = await plugin.store.get_profile(aff_umo, "u-aff")
    check("改画像后备注生效", (prof2 or {}).get("note") == "老朋友", str(prof2))
    check(
        "改画像后好感度生效",
        abs(float((prof2 or {}).get("affection") or 0) - 42.0) < 0.01,
        str((prof2 or {}).get("affection")),
    )
    check(
        "手动改的好感度立刻读到",
        abs(await plugin._affection_of(aff_umo, "u-aff") - 42.0) < 0.01,
    )

    # 23.3 手动改的昵称不会被后续互动覆盖
    await plugin.store.update_profile_fields(aff_umo, "u-aff", nickname="宝哥")
    await plugin.store.touch_profile(
        aff_umo, "u-aff", nickname="平台昵称", event="user_message"
    )
    prof3 = await plugin.store.get_profile(aff_umo, "u-aff")
    check("手动昵称不被覆盖", (prof3 or {}).get("nickname") == "宝哥", str(prof3))

    # 23.4 历史聊天记录回填到画像
    hist_umo = "aiocqhttp:FriendMessage:hist1"
    for i in range(3):
        await plugin.store.add_message(
            hist_umo, "user", f"历史消息{i}", "老用户", "u-hist"
        )
    await plugin.store.add_message(hist_umo, "bot", "在的", "bot", "")
    count = await plugin.store.backfill_profiles()
    check("历史回填有产出", count >= 1, str(count))
    hist_profile = await plugin.store.get_profile(hist_umo, "u-hist")
    check("历史用户生成画像", hist_profile is not None)
    check(
        "画像包含历史消息数",
        int((hist_profile or {}).get("msg_count") or 0) == 3,
        str((hist_profile or {}).get("msg_count")),
    )
    check(
        "画像包含历史机器人回应数",
        int((hist_profile or {}).get("bot_replies") or 0) >= 1,
        str((hist_profile or {}).get("bot_replies")),
    )
    check(
        "历史用户也有好感度",
        float((hist_profile or {}).get("affection") or 0) > 0,
        str((hist_profile or {}).get("affection")),
    )
    hist_text = await plugin._render_profile(hist_profile or {})
    check("画像展示历史聊天记录", "历史消息" in hist_text, hist_text[:300])

    # 23.5 画像里能看到聊天足迹
    await plugin.store.add_message(aff_umo, "user", "今天天气不错", "阿宝", "u-aff")
    aff_text = await plugin._render_profile(
        await plugin.store.get_profile(aff_umo, "u-aff") or {}
    )
    check("画像展示最近说过的话", "今天天气不错" in aff_text, aff_text[:400])

    # 23.6 指令仅限管理员使用
    cmds = getattr(plugin.companion, "commands", {})
    check("指令组下有 13 条子指令", len(cmds) == 13, str(sorted(cmds)))
    non_admin = [
        name
        for name, func in cmds.items()
        if getattr(func, "_permission_type", None) != "admin"
    ]
    check("所有指令都限制为管理员", not non_admin, str(non_admin))

    print("\n== 24. 画像按人合并与同步写入 ==")
    prof_req = MockRequest()
    request_holder["current"] = prof_req
    prof_req.set_query()
    # 同一个用户在两个会话里各有一份画像
    await plugin.store.touch_profile(
        "aiocqhttp:GroupMessage:g1", "u-duo", nickname="多多", event="user_message"
    )
    await plugin.store.touch_profile(
        "aiocqhttp:FriendMessage:f1", "u-duo", nickname="多多", event="user_message"
    )
    resp = await plugin.api_profiles()
    items = resp.payload["profiles"]
    duo = [p for p in items if p["user_id"] == "u-duo"]
    check("同一个人合并成一条画像", len(duo) == 1, str([p["user_id"] for p in items]))
    check("合并后统计到会话数", duo and duo[0]["session_count"] == 2, str(duo))
    check("合并后消息数相加", duo and duo[0]["msg_count"] == 2, str(duo))
    check("画像带人物速写", duo and bool(duo[0].get("summary")), str(duo))
    check("画像带聊天足迹字段", duo and "recent_messages" in duo[0], str(duo))

    # 面板保存 → 所有会话同步
    prof_req.set_json(
        {
            "session_key": "aiocqhttp:GroupMessage:g1",
            "user_id": "u-duo",
            "nickname": "多多名",
            "tags": "同事,话痨",
            "affection": 55,
            "note": "很爱聊",
        }
    )
    resp = await plugin.api_update_profile()
    check("面板保存成功", resp.payload.get("saved") is True, str(resp.payload))
    check(
        "保存返回同步的会话数",
        (resp.payload.get("applied") or {}).get("sessions") == 2,
        str(resp.payload.get("applied")),
    )
    rows = await plugin.store.user_profile_sessions("u-duo")
    check(
        "所有会话昵称都同步",
        all(r.get("nickname") == "多多名" for r in rows),
        str(rows),
    )
    check("所有会话备注都同步", all(r.get("note") == "很爱聊" for r in rows), str(rows))
    check(
        "所有会话好感度都同步",
        all(float(r.get("affection") or 0) == 55.0 for r in rows),
        str(rows),
    )
    check(
        "所有会话标签都同步",
        all("话痨" in str(r.get("tags") or "") for r in rows),
        str(rows),
    )

    # 没有画像行的人 → 自动补一份
    prof_req.set_json(
        {
            "session_key": "aiocqhttp:FriendMessage:new",
            "user_id": "u-new",
            "note": "新来的",
        }
    )
    resp = await plugin.api_update_profile()
    check(
        "不存在的画像会自动创建", resp.payload.get("saved") is True, str(resp.payload)
    )
    new_row = await plugin.store.get_profile("aiocqhttp:FriendMessage:new", "u-new")
    check("新画像已落库", (new_row or {}).get("note") == "新来的", str(new_row))

    # 画像渲染包含互动概况与速写
    text = await plugin._render_profile(new_row or {})
    check("画像含互动概况", "互动概况" in text, text[:200])
    check("画像含速写", "速写" in text, text[:300])
    prof_req.set_json({})

    print("\n== 25. 一键清除记忆 ==")
    await plugin.store.ensure_session(
        aff_umo, aff_umo, "aiocqhttp", "GroupMessage", "测试会话"
    )
    # 当前会话：消息 + 画像 + 情绪
    await plugin.store.add_message(aff_umo, "user", "清一清", "阿宝", "u-aff")
    await plugin.store.touch_profile(
        aff_umo, "u-aff", nickname="阿宝", event="user_message"
    )
    await plugin.store.update_session(aff_umo, valence=0.5, arousal=0.9)
    cleared = await plugin.store.clear_session_memory(aff_umo)
    check("会话消息已清除", cleared["messages"] >= 1, str(cleared))
    check("会话画像已清除", cleared["profiles"] >= 1, str(cleared))
    check(
        "会话消息确实没了",
        len(await plugin.store.recent_messages(aff_umo, 50)) == 0,
    )
    check(
        "会话画像确实没了",
        (await plugin.store.get_profile(aff_umo, "u-aff")) is None,
    )
    session_after = await plugin.store.get_session(aff_umo)
    check("会话情绪已重置", float(session_after.get("valence") or 0) == 0.0)

    # 某个人：画像 + 好感度 + 他说过的话
    await plugin.store.add_message(aff_umo, "user", "我来了", "阿宝", "u-aff")
    await plugin.store.add_message(aff_umo, "bot", "欢迎", "bot", "")
    await plugin.store.touch_profile(
        aff_umo, "u-aff", nickname="阿宝", event="user_message"
    )
    await plugin.store.touch_profile(
        aff_umo, "u-other", nickname="别人", event="user_message"
    )
    cleared = await plugin.store.clear_user_memory("u-aff")
    check("用户画像已清除", cleared["profiles"] == 1, str(cleared))
    check("用户消息已清除", cleared["messages"] == 1, str(cleared))
    check(
        "别人的画像不受影响",
        (await plugin.store.get_profile(aff_umo, "u-other")) is not None,
    )
    check(
        "只清画像时保留聊天记录",
        (await plugin.store.clear_user_memory("u-other", keep_messages=True))[
            "messages"
        ]
        == 0,
    )
    check(
        "只清画像时聊天记录还在",
        len(await plugin.store.recent_messages(aff_umo, 50)) >= 1,
    )

    # 全部
    await plugin.store.add_message(aff_umo, "user", "最后一条", "阿宝", "u-aff")
    cleared = await plugin.store.clear_all_memory()
    check("全部清除有统计", cleared["messages"] >= 1, str(cleared))
    check(
        "全部画像都没了",
        len(await plugin.store.list_profiles(None)) == 0,
    )

    # 指令：不带参数 = 当前会话
    await plugin.store.add_message(aff_umo, "user", "你好", "阿宝", "u-aff")
    await plugin.store.touch_profile(
        aff_umo, "u-aff", nickname="阿宝", event="user_message"
    )
    res = await mock_await_result(
        plugin.companion_forget(MockEvent(aff_umo, "陪伴 清除记忆"))
    )
    check("清除记忆指令有回复", res is not None and "已清除" in res[1], str(res))
    check(
        "当前会话消息确实被清空",
        len(await plugin.store.recent_messages(aff_umo, 50)) == 0,
    )

    # 指令：某个人
    await plugin.store.add_message(aff_umo, "user", "我来了", "阿宝", "u-aff")
    await plugin.store.touch_profile(
        aff_umo, "u-aff", nickname="阿宝", event="user_message"
    )
    res = await mock_await_result(
        plugin.companion_forget(MockEvent(aff_umo, "陪伴 清除记忆 u-aff"))
    )
    check("清除某人记忆有回复", res is not None and "已清除" in res[1], str(res))
    check(
        "某人画像被清除",
        (await plugin.store.get_profile(aff_umo, "u-aff")) is None,
    )

    # 指令：只清画像
    await plugin.store.add_message(aff_umo, "user", "留着这句话", "阿宝", "u-aff")
    await plugin.store.touch_profile(
        aff_umo, "u-aff", nickname="阿宝", event="user_message"
    )
    res = await mock_await_result(
        plugin.companion_forget(MockEvent(aff_umo, "陪伴 清除记忆 画像 u-aff"))
    )
    check("只清画像有回复", res is not None and "已清除" in res[1], str(res))
    check(
        "只清画像后聊天记录保留",
        len(await plugin.store.recent_messages(aff_umo, 50)) >= 1,
    )
    check(
        "只清画像后画像没了",
        (await plugin.store.get_profile(aff_umo, "u-aff")) is None,
    )

    # 指令：全部
    res = await mock_await_result(
        plugin.companion_forget(MockEvent(aff_umo, "陪伴 清除记忆 全部"))
    )
    check("清除全部有回复", res is not None and "已清除" in res[1], str(res))
    check(
        "全部消息被清空",
        len(await plugin.store.recent_messages(aff_umo, 50)) == 0,
    )

    # Web API
    prof_req.set_json({"scope": "session", "session_key": aff_umo})
    resp = await plugin.api_clear_memory()
    check("清除记忆接口可用", resp.payload.get("cleared") is True, str(resp.payload))
    prof_req.set_json({"scope": "bad"})
    resp = await plugin.api_clear_memory()
    check("非法 scope 报错", resp.status_code == 400, str(resp.status_code))
    prof_req.set_json({})

    print("\n== 26. 昵称跟随与称呼规则 ==")
    from core import emotion as emotion_mod  # type: ignore
    from core import generator as gen  # type: ignore

    # 改了昵称后，历史消息里的旧名字要被替换成新名字
    nick_umo = "aiocqhttp:FriendMessage:nick1"
    await plugin.store.add_message(nick_umo, "user", "在吗", "旧名字", "u-nick")
    await plugin.store.add_message(nick_umo, "user", "还是我", "新名字", "u-nick")
    names, peer_id, peer_name = await plugin.store.latest_sender_names(nick_umo)
    check("能取到最新昵称", names.get("u-nick") == "新名字", str(names))
    check(
        "能取到最近发言者",
        peer_id == "u-nick" and peer_name == "新名字",
        f"{peer_id}/{peer_name}",
    )

    block = gen.build_context_block(
        await plugin.store.recent_messages(nick_umo, 10), names=names
    )
    check("上下文里用新昵称", "新名字:" in block, block[:120])
    check("上下文里不再出现旧昵称", "旧名字" not in block, block[:120])

    # 主动消息的上下文也要带昵称
    check(
        "主动消息上下文带昵称",
        "新名字: 还是我" in block,
        block[:120],
    )

    # 提示词里要有称呼规则
    prompt = gen.build_prompt(
        block,
        emotion_mod.EmotionState(),
        30.0,
        is_group=False,
        peer_name="新名字",
    )
    check("提示词带昵称", "新名字" in prompt, prompt[:200])
    check(
        "提示词禁止泛称",
        "亲爱的" in prompt and "不要用" in prompt,
        prompt[:300],
    )

    # 主动回复注入的上下文也要带称呼规则
    inject_block = plugin._format_context_block(
        await plugin.store.recent_messages(nick_umo, 10),
        "",
        names,
        "新名字",
    )
    check("注入上下文带称呼规则", "不要用" in inject_block, inject_block[-300:])

    # 手动昵称优先；留空则恢复跟随平台昵称
    await plugin.store.touch_profile(
        nick_umo, "u-nick", nickname="平台昵称", event="user_message"
    )
    await plugin.store.update_profile_fields_by_user(
        "u-nick", nick_umo, nickname="宝哥"
    )
    profile_nick = await plugin.store.get_profile(nick_umo, "u-nick")
    check(
        "手动昵称已保存",
        (profile_nick or {}).get("nickname") == "宝哥",
        str(profile_nick),
    )
    check(
        "手动昵称标记已置位",
        int((profile_nick or {}).get("nickname_manual") or 0) == 1,
        str(profile_nick),
    )
    await plugin.store.touch_profile(
        nick_umo, "u-nick", nickname="平台昵称2", event="user_message"
    )
    profile_nick = await plugin.store.get_profile(nick_umo, "u-nick")
    check(
        "手动昵称不被平台覆盖",
        (profile_nick or {}).get("nickname") == "宝哥",
        str(profile_nick),
    )

    # 手动昵称也会覆盖上下文里的名字
    names2, _, peer2 = await plugin._nickname_context(nick_umo)
    check("手动昵称优先于平台昵称", peer2 == "宝哥", f"{peer2}")

    # 清空昵称 = 恢复跟随平台昵称
    await plugin.store.update_profile_fields_by_user("u-nick", nick_umo, nickname="")
    profile_nick = await plugin.store.get_profile(nick_umo, "u-nick")
    check(
        "清空昵称后取消手动标记",
        int((profile_nick or {}).get("nickname_manual") or 0) == 0,
        str(profile_nick),
    )
    await plugin.store.touch_profile(
        nick_umo, "u-nick", nickname="平台昵称3", event="user_message"
    )
    profile_nick = await plugin.store.get_profile(nick_umo, "u-nick")
    check(
        "恢复后跟随平台昵称",
        (profile_nick or {}).get("nickname") == "平台昵称3",
        str(profile_nick),
    )

    print("\n== 27. 私聊专属主动策略 ==")
    # 27.1 私聊阈值独立于群聊
    plugin.config["min_idle_minutes"] = 45
    plugin.config["cooldown_minutes"] = 180
    plugin.config["max_daily_messages"] = 6
    plugin.config["private_min_idle_minutes"] = 15
    plugin.config["private_cooldown_minutes"] = 60
    plugin.config["private_max_daily_messages"] = 10
    private_limits = plugin._effective_limits("private")
    group_limits = plugin._effective_limits("group")
    check("私聊静默阈值更短", private_limits["min_idle"] == 15, str(private_limits))
    check("私聊冷却更短", private_limits["cooldown"] == 60, str(private_limits))
    check("私聊每日额度更高", private_limits["max_daily"] == 10, str(private_limits))
    check(
        "群聊阈值不受影响",
        group_limits["min_idle"] == 45 and group_limits["cooldown"] == 180,
        str(group_limits),
    )

    # 27.2 设为 0 时回落到全局
    plugin.config["private_min_idle_minutes"] = 0
    plugin.config["private_cooldown_minutes"] = 0
    plugin.config["private_max_daily_messages"] = 0
    fallback = plugin._effective_limits("private")
    check(
        "私聊设为 0 时跟随全局",
        fallback["min_idle"] == 45
        and fallback["cooldown"] == 180
        and fallback["max_daily"] == 6,
        str(fallback),
    )

    # 27.3 私聊免打扰时段独立
    plugin.config["private_quiet_hours"] = ["23:30-08:00"]
    quiet_limits = plugin._effective_limits("private")
    check(
        "私聊使用自己的免打扰时段",
        quiet_limits["quiet_hours"] == ["23:30-08:00"],
        str(quiet_limits),
    )
    check(
        "群聊免打扰时段不受影响",
        plugin._effective_limits("group")["quiet_hours"] == [],
        str(plugin._effective_limits("group")),
    )
    plugin.config["private_quiet_hours"] = []

    # 27.4 私聊优先调度
    plugin.config["private_priority"] = True
    ordered = plugin._order_sessions(
        [
            {"session_key": "g1", "chat_type": "group"},
            {"session_key": "p1", "chat_type": "private"},
            {"session_key": "g2", "chat_type": "group"},
            {"session_key": "p2", "chat_type": "private"},
        ]
    )
    types = [str(s.get("chat_type")) for s in ordered]
    check("私聊排在群聊前面", types[:2] == ["private", "private"], str(types))
    check("群聊排在后面", types[2:] == ["group", "group"], str(types))
    plugin.config["private_priority"] = False
    ordered2 = plugin._order_sessions(
        [
            {"session_key": "g1", "chat_type": "group"},
            {"session_key": "p1", "chat_type": "private"},
        ]
    )
    check(
        "关闭优先后顺序打散",
        len(ordered2) == 2
        and {str(s.get("chat_type")) for s in ordered2} == {"group", "private"},
        str(ordered2),
    )
    plugin.config["private_priority"] = True

    # 27.5 私聊不受气氛规则限制（默认开启）
    check(
        "默认私聊跳过气氛判定",
        plugin._cfg_bool("private_skip_atmosphere", True) is True,
    )
    plugin.config["private_skip_atmosphere"] = False
    check(
        "可关闭私聊跳过气氛",
        plugin._cfg_bool("private_skip_atmosphere", True) is False,
    )
    plugin.config["private_skip_atmosphere"] = True

    # 27.6 私聊提示词更口语化
    priv_prompt = gen.build_prompt(
        "用户: 在吗", emotion_mod.EmotionState(), 20.0, is_group=False
    )
    check("私聊提示词提到一对一", "一对一" in priv_prompt, priv_prompt[:200])

    # 27.7 面板可读写私聊配置
    cfg_req = MockRequest()
    request_holder["current"] = cfg_req
    cfg_req.set_json(
        {
            "config": {
                "private_min_idle_minutes": 20,
                "private_cooldown_minutes": 90,
                "private_max_daily_messages": 12,
                "private_skip_atmosphere": False,
                "private_priority": False,
            }
        }
    )
    resp = await plugin.api_save_config()
    check("面板保存私聊配置成功", resp.payload.get("saved") is True, str(resp.payload))
    saved = resp.payload.get("config") or {}
    check("私聊静默阈值已保存", saved.get("private_min_idle_minutes") == 20, str(saved))
    check(
        "私聊跳过气氛已保存", saved.get("private_skip_atmosphere") is False, str(saved)
    )
    check(
        "保存后立即生效",
        plugin._effective_limits("private")["min_idle"] == 20,
        str(plugin._effective_limits("private")),
    )
    cfg_req.set_json({})

    # 27.8 私聊里即使气氛「紧张」也照样主动开口
    from core import atmosphere as atmo_mod  # type: ignore

    tense_umo = "aiocqhttp:FriendMessage:tense1"
    await plugin.store.ensure_session(
        tense_umo, tense_umo, "aiocqhttp", "private", "私聊会话"
    )
    await plugin.store.update_session(tense_umo, enabled=1)
    now2 = time.time()
    plugin._atmosphere_cache[tense_umo] = atmo_mod.analyze(
        [
            {
                "content": "你真傻逼滚蛋",
                "sender": "u",
                "sender_id": "u",
                "ts": now2,
                "role": "user",
            }
        ],
        now=now2,
    )
    check(
        "测试用气氛确实被判为紧张",
        plugin._atmosphere_cache[tense_umo].level == atmo_mod.LEVEL_TENSE,
        plugin._atmosphere_cache[tense_umo].level,
    )
    plugin.config["min_idle_minutes"] = 0
    plugin.config["cooldown_minutes"] = 0
    plugin.config["max_daily_messages"] = 50
    plugin.config["max_proactive_per_tick"] = 50
    plugin.config["private_min_idle_minutes"] = 0
    plugin.config["private_cooldown_minutes"] = 0
    plugin.config["private_max_daily_messages"] = 0
    plugin.config["private_skip_atmosphere"] = True
    ctx.sent.clear()
    await plugin.tick()
    check(
        "私聊不受气氛紧张影响，照样主动搭话",
        len(ctx.sent_to(tense_umo)) == 1,
        f"sent={len(ctx.sent_to(tense_umo))}",
    )

    # 反过来：群聊仍受气氛影响
    plugin.config["private_skip_atmosphere"] = False
    ctx.sent.clear()
    await plugin.tick()
    check(
        "群聊仍受气氛规则约束",
        len(ctx.sent_to(tense_umo)) == 0,
        f"sent={len(ctx.sent_to(tense_umo))}",
    )
    plugin.config["private_skip_atmosphere"] = True
    plugin._atmosphere_cache.pop(tense_umo, None)

    # 27.9 私聊主动搭话白名单
    wl_umo = "aiocqhttp:FriendMessage:wl1"
    await plugin.store.ensure_session(
        wl_umo, wl_umo, "aiocqhttp", "private", "私聊白名单"
    )
    await plugin.store.update_session(wl_umo, enabled=1)
    await plugin.store.add_message(wl_umo, "user", "在吗", "小白", "u-wl")

    # 白名单为空 = 不限制
    plugin.config["private_proactive_whitelist"] = []
    check(
        "白名单为空时放行",
        await plugin._private_whitelisted({"session_key": wl_umo, "umo": wl_umo}),
    )

    # 白名单里是别人 → 该私聊保持安静
    plugin.config["private_proactive_whitelist"] = ["u-other"]
    check(
        "非白名单用户不放行",
        not await plugin._private_whitelisted({"session_key": wl_umo, "umo": wl_umo}),
    )
    plugin.config["max_daily_messages"] = 50
    plugin.config["max_proactive_per_tick"] = 50
    ctx.sent.clear()
    await plugin.tick()
    check(
        "非白名单用户的私聊不会被主动搭话",
        len(ctx.sent_to(wl_umo)) == 0,
        f"sent={len(ctx.sent_to(wl_umo))}",
    )

    # 白名单里是对方用户 ID → 放行
    plugin.config["private_proactive_whitelist"] = ["u-wl"]
    check(
        "白名单命中用户 ID 放行",
        await plugin._private_whitelisted({"session_key": wl_umo, "umo": wl_umo}),
    )
    ctx.sent.clear()
    await plugin.tick()
    check(
        "白名单用户的私聊会被主动搭话",
        len(ctx.sent_to(wl_umo)) == 1,
        f"sent={len(ctx.sent_to(wl_umo))}",
    )

    # 白名单里是昵称 → 同样放行
    plugin.config["private_proactive_whitelist"] = ["小白"]
    check(
        "白名单命中昵称放行",
        await plugin._private_whitelisted({"session_key": wl_umo, "umo": wl_umo}),
    )

    # `*` = 不限制
    plugin.config["private_proactive_whitelist"] = ["*"]
    check(
        "星号表示不限制",
        await plugin._private_whitelisted({"session_key": wl_umo, "umo": wl_umo}),
    )
    plugin.config["private_proactive_whitelist"] = []

    # 恢复默认，避免影响其它用例
    plugin.config["min_idle_minutes"] = 0
    plugin.config["cooldown_minutes"] = 0
    plugin.config["max_daily_messages"] = 3
    plugin.config["max_proactive_per_tick"] = 3
    plugin.config["private_min_idle_minutes"] = 0
    plugin.config["private_cooldown_minutes"] = 0
    plugin.config["private_max_daily_messages"] = 0
    plugin.config["private_skip_atmosphere"] = True
    plugin.config["private_priority"] = True

    print("\n== 28. 上下文注入护栏与自动熔断 ==")
    # 28.1 单条消息超长会被截断
    plugin.config["context_max_message_chars"] = 10
    plugin.config["context_blocked_keywords"] = []
    long_msgs = [{"content": "啊" * 50, "role": "user", "sender": "甲"}]
    trimmed = plugin._sanitize_messages(long_msgs)
    check("超长消息被截断", len(trimmed[0]["content"]) == 11, str(trimmed))
    check(
        "截断后保留开头",
        trimmed[0]["content"].startswith("啊啊啊啊啊啊"),
        trimmed[0]["content"],
    )

    # 28.2 命中屏蔽词的消息整条丢弃
    plugin.config["context_blocked_keywords"] = ["违禁"]
    filtered = plugin._sanitize_messages(
        [
            {"content": "普通消息", "role": "user", "sender": "甲"},
            {"content": "这里有违禁内容", "role": "user", "sender": "乙"},
        ]
    )
    check("命中屏蔽词的消息被丢弃", len(filtered) == 1, str(filtered))
    check("保留的是没命中的那条", filtered[0]["content"] == "普通消息", str(filtered))
    plugin.config["context_blocked_keywords"] = []

    # 28.3 注入总长上限
    plugin.config["context_max_chars"] = 200
    plugin.config["context_max_message_chars"] = 200
    many = [
        {"content": f"第{i}条消息内容", "role": "user", "sender": f"u{i}"}
        for i in range(50)
    ]
    block = plugin._format_context_block(many, "", {}, "")
    body = block.split("<chat_context>")[1].split("</chat_context>")[0]
    check("注入总长被限制", len(body) <= 260, f"len={len(body)}")

    # 28.4 LLM 输入被拦的错误能被识别
    check(
        "识别 All chat models failed",
        plugin._looks_like_llm_rejection(
            "LLM 响应错误: All chat models failed: InternalServerError: 500 - "
            "{'error': {'message': 'input new_sensitive (1026)'}}"
        ),
    )
    check(
        "识别 sensitive 关键词",
        plugin._looks_like_llm_rejection("input new_sensitive (1026)"),
    )
    check(
        "普通回复不被误判",
        not plugin._looks_like_llm_rejection("今天天气不错呀"),
    )

    # 28.5 出现拦截错误时自动熔断该会话的上下文注入
    guard_umo = "aiocqhttp:GroupMessage:guard"
    await plugin.store.ensure_session(
        guard_umo, guard_umo, "aiocqhttp", "group", "护栏群"
    )
    await plugin.store.update_session(guard_umo, enabled=1)
    check(
        "初始未熔断",
        not await plugin._context_inject_disabled(guard_umo),
    )
    err_event = MockEvent(guard_umo, "随便说句")
    err_event.set_result(
        "LLM 响应错误: All chat models failed: InternalServerError: 500 - "
        "{'error': {'message': 'input new_sensitive (1026)'}}"
    )
    await plugin.capture_turn(err_event)
    check(
        "检测到拦截后自动熔断",
        await plugin._context_inject_disabled(guard_umo),
    )
    guard_msgs = await plugin.store.recent_messages(guard_umo, 20)
    check(
        "错误提示不会被写进聊天记录",
        not any(
            "All chat models failed" in str(m.get("content", "")) for m in guard_msgs
        ),
        str(guard_msgs),
    )
    check(
        "错误时不会记录 bot 回复",
        not any(m.get("role") in ("bot", "proactive") for m in guard_msgs),
        str(guard_msgs),
    )
    events = await plugin.store.recent_events(50)
    check(
        "记录了 llm_input_rejected 事件",
        any(e.get("kind") == "llm_input_rejected" for e in events),
        str([e.get("kind") for e in events[:5]]),
    )

    # 28.6 可以手动恢复
    await plugin._set_context_inject(guard_umo, False)
    check(
        "手动恢复后不再熔断",
        not await plugin._context_inject_disabled(guard_umo),
    )

    # 28.7 指令可以查看 / 暂停 / 恢复
    res = await mock_await_result(
        plugin.companion_context(MockEvent(guard_umo, "陪伴 上下文"))
    )
    check("上下文指令有回复", res is not None and "上下文注入状态" in res[1], str(res))
    res = await mock_await_result(
        plugin.companion_context(MockEvent(guard_umo, "陪伴 上下文 暂停"))
    )
    check(
        "上下文指令可暂停",
        res is not None and "已暂停" in res[1],
        str(res),
    )
    check(
        "暂停后处于熔断状态",
        await plugin._context_inject_disabled(guard_umo),
    )
    res = await mock_await_result(
        plugin.companion_context(MockEvent(guard_umo, "陪伴 上下文 恢复"))
    )
    check(
        "上下文指令可恢复",
        res is not None and "已恢复" in res[1],
        str(res),
    )
    check(
        "恢复后不再熔断",
        not await plugin._context_inject_disabled(guard_umo),
    )

    plugin.config["context_max_chars"] = 1200
    plugin.config["context_max_message_chars"] = 200
    plugin.config["context_blocked_keywords"] = []
    plugin.config["context_injection_enabled"] = True

    print("\n== 29. 聊天记录保存条数与保留天数 ==")
    keep_umo = "grp:keep_test"

    # 29.1 写入后立即按条数裁剪（不需要等定时任务）
    plugin.config["keep_messages_per_session"] = 10
    plugin.config["keep_messages_days"] = 0
    for i in range(15):
        await plugin.store.add_message(keep_umo, "user", f"第{i}条", "甲", "u-keep")
        await plugin.store.prune_session_messages(keep_umo, 10)
    kept = await plugin.store.recent_messages(keep_umo, 100)
    check(
        "写入后立即按条数裁剪",
        len(kept) == 10,
        f"实际 {len(kept)} 条",
    )
    check(
        "裁剪后保留的是最近的记录",
        kept[-1]["content"] == "第14条",
        str(kept[-1] if kept else None),
    )

    # 29.2 超过保留天数的记录会被清掉
    old_ts = time.time() - 40 * 86400
    await plugin.store.add_message(
        keep_umo, "user", "很久以前", "甲", "u-keep", ts=old_ts
    )
    removed = await plugin.store.prune_session_messages_by_age(keep_umo, 30)
    check("按天数清理过期记录", removed == 1, f"删除 {removed} 条")
    check(
        "过期记录已被删除",
        all(
            m["content"] != "很久以前"
            for m in await plugin.store.recent_messages(keep_umo, 100)
        ),
    )

    # 29.3 天数为 0 时不按时间清理
    await plugin.store.add_message(
        keep_umo, "user", "更久以前", "甲", "u-keep", ts=old_ts
    )
    removed0 = await plugin.store.prune_session_messages_by_age(keep_umo, 0)
    check(
        "天数为 0 时不清理",
        removed0 == 0
        and any(
            m["content"] == "更久以前"
            for m in await plugin.store.recent_messages(keep_umo, 100)
        ),
        f"删除 {removed0} 条",
    )

    # 29.4 全局按天数清理
    removed_all = await plugin.store.prune_messages_by_age(30)
    check(
        "全局按天数清理",
        removed_all >= 1
        and all(
            m["content"] != "更久以前"
            for m in await plugin.store.recent_messages(keep_umo, 100)
        ),
        f"删除 {removed_all} 条",
    )
    check(
        "全局天数为 0 时不清理",
        await plugin.store.prune_messages_by_age(0) == 0,
    )

    # 29.5 诊断里能看到当前生效的保存设置
    plugin.config["keep_messages_per_session"] = 120
    plugin.config["keep_messages_days"] = 15
    diag = await mock_await_result(
        plugin.companion_diagnose(MockEvent(keep_umo, "陪伴 诊断"))
    )
    check(
        "诊断显示聊天记录保存设置",
        diag is not None and "聊天记录保存" in diag[1] and "120" in diag[1],
        str(diag),
    )

    # 29.6 面板概览里能看到保存设置
    state_resp = await plugin.api_state()
    state = state_resp.payload
    check(
        "概览接口带聊天记录保存设置",
        state.get("keep_messages_per_session") == 120
        and state.get("keep_messages_days") == 15,
        str(
            {
                "keep_messages_per_session": state.get("keep_messages_per_session"),
                "keep_messages_days": state.get("keep_messages_days"),
            }
        ),
    )

    plugin.config["keep_messages_per_session"] = 200
    plugin.config["keep_messages_days"] = 30

    print("\n== 30. 重复消息与重复投递防护 ==")
    dup_umo = "grp:dup_test"

    # 30.1 短窗口内同一条消息只入库一次
    ok1 = await plugin.store.add_message(
        dup_umo, "user", "只应记一次", "甲", "u-dup", dedupe_window=30.0
    )
    ok2 = await plugin.store.add_message(
        dup_umo, "user", "只应记一次", "甲", "u-dup", dedupe_window=30.0
    )
    dup_rows = await plugin.store.recent_messages(dup_umo, 50)
    check("重复写入被拦下", ok1 and not ok2, f"{ok1} {ok2}")
    check(
        "同一句话只入库一次",
        sum(1 for m in dup_rows if m["content"] == "只应记一次") == 1,
        str(dup_rows),
    )

    # 30.2 不开启去重时照常写入
    await plugin.store.add_message(dup_umo, "user", "只应记一次", "甲", "u-dup")
    dup_rows2 = await plugin.store.recent_messages(dup_umo, 50)
    check(
        "关闭去重时仍然写入",
        sum(1 for m in dup_rows2 if m["content"] == "只应记一次") == 2,
        str(dup_rows2),
    )

    # 30.3 不同发送者的相同内容不算重复
    await plugin.store.add_message(
        dup_umo, "user", "只应记一次", "乙", "u-other", dedupe_window=30.0
    )
    dup_rows3 = await plugin.store.recent_messages(dup_umo, 50)
    check(
        "不同人说的同一句话各自保留",
        sum(1 for m in dup_rows3 if m["content"] == "只应记一次") == 3,
        str(dup_rows3),
    )

    # 30.4 三条写入路径都走同一条消息时，最终只记一条
    path_umo = "grp:dup_paths"
    await plugin._record_user_message(path_umo, "同一句话", "甲", "u-paths")
    await plugin._record_user_message(path_umo, "同一句话", "甲", "u-paths")
    await plugin._record_user_message(path_umo, "同一句话", "甲", "u-paths")
    path_rows = await plugin.store.recent_messages(path_umo, 50)
    check(
        "多条路径写入同一条消息只留一条",
        len(path_rows) == 1,
        str(path_rows),
    )

    # 30.5 同一条消息被重复投递时不会连续触发主动回复
    plugin.config["proactive_reply_enabled"] = True
    plugin.config["proactive_reply_probability"] = 1.0
    plugin.config["proactive_reply_cooldown_minutes"] = 0
    plugin.config["proactive_reply_min_length"] = 2
    plugin._proactive_reply_times.clear()
    plugin._reply_triggered_message_ids.clear()
    dup_event = MockEvent(
        dup_umo, "重复投递的消息", chat_type="GroupMessage", message_id="m-123"
    )
    first = plugin.should_proactive_reply(dup_event, None)
    second = plugin.should_proactive_reply(dup_event, None)
    third = plugin.should_proactive_reply(dup_event, None)
    check("首次投递可以触发", first, f"{first}")
    check("重复投递不再触发", not second and not third, f"{second} {third}")

    # 30.6 没有 message_id 时靠冷却挡住重复
    plugin._proactive_reply_times.clear()
    plugin._reply_triggered_message_ids.clear()
    plugin.config["proactive_reply_cooldown_minutes"] = 10
    no_mid = MockEvent(dup_umo, "没有消息ID", chat_type="GroupMessage")
    check(
        "无消息ID时第一次触发",
        plugin.should_proactive_reply(no_mid, None),
    )
    check(
        "无消息ID时第二次被冷却挡住",
        not plugin.should_proactive_reply(no_mid, None),
    )
    plugin.config["proactive_reply_cooldown_minutes"] = 10
    plugin.config["proactive_reply_probability"] = 0.2
    plugin._proactive_reply_times.clear()
    plugin._reply_triggered_message_ids.clear()

    print("\n== 31. 上下文重复内容去重 ==")

    # 31.1 注入的上下文里，相同内容只出现一次
    ctx_umo = "grp:ctx_dup"
    for _ in range(3):
        await plugin.store.add_message(ctx_umo, "user", "我们到了吗", "甲", "u-c")
    dup_block = plugin._format_context_block(
        await plugin.store.recent_messages(ctx_umo, 20), "", {}, ""
    )
    check(
        "注入块内重复内容只保留一条",
        dup_block.count("我们到了吗") == 1,
        dup_block[:200],
    )

    # 31.2 主动消息上下文（含 AstrBot 历史）同样去重
    gen_block = main_mod.generator.build_context_block(
        [
            {
                "role": "user",
                "content": "我们到了吗",
                "sender": "甲",
                "sender_id": "u-c",
            },
            {
                "role": "user",
                "content": "我们到了吗",
                "sender": "甲",
                "sender_id": "u-c",
            },
        ],
        ["甲: 我们到了吗"],
        max_items=12,
    )
    check(
        "主动消息上下文去重",
        gen_block.count("我们到了吗") == 1,
        gen_block[:200],
    )

    # 31.3 LLM 请求历史里的重复消息会被合并
    class _Req2:
        def __init__(self) -> None:
            self.extra_user_content_parts: list[Any] = []
            self.contexts: list[Any] = [
                {"role": "user", "content": "同一句话"},
                {"role": "user", "content": "同一句话"},
                {"role": "assistant", "content": "收到"},
                {"role": "user", "content": "同一句话"},
                {"role": "assistant", "content": "收到"},
            ]

    req3 = _Req2()
    removed = plugin._dedupe_request_contexts(req3)
    check("请求历史去重条数正确", removed == 3, f"removed={removed}")
    check(
        "请求历史只剩唯一内容",
        [c["content"] for c in req3.contexts] == ["同一句话", "收到"],
        str(req3.contexts),
    )

    # 31.4 不同角色的相同内容不会被误合并
    class _Req3:
        def __init__(self) -> None:
            self.extra_user_content_parts: list[Any] = []
            self.contexts: list[Any] = [
                {"role": "user", "content": "好"},
                {"role": "assistant", "content": "好"},
            ]

    req4 = _Req3()
    plugin._dedupe_request_contexts(req4)
    check(
        "不同角色相同内容保留",
        len(req4.contexts) == 2,
        str(req4.contexts),
    )

    # 31.5 关闭开关后不再去重
    plugin.config["dedupe_llm_context_enabled"] = False

    class _Req5:
        def __init__(self) -> None:
            self.extra_user_content_parts: list[Any] = []
            self.contexts: list[Any] = [
                {"role": "user", "content": "同一句话"},
                {"role": "user", "content": "同一句话"},
            ]

    req5 = _Req5()
    plugin._dedupe_request_contexts(req5)
    check(
        "关闭后不去重",
        len(req5.contexts) == 2,
        str(req5.contexts),
    )
    plugin.config["dedupe_llm_context_enabled"] = True

    # 31.8 库里的重复记录可以被清理
    dup2_umo = "grp:dup_clean"
    now = time.time()
    for offset in (0, 1, 2):
        await plugin.store.add_message(
            dup2_umo, "user", "同一句", "甲", "u-dc", ts=now - offset
        )
    # 很久以前说的同一句不算重复
    await plugin.store.add_message(
        dup2_umo, "user", "同一句", "甲", "u-dc", ts=now - 86400
    )
    before = len(await plugin.store.recent_messages(dup2_umo, 50))
    removed_dup = await plugin.store.prune_duplicate_messages()
    after_rows = await plugin.store.recent_messages(dup2_umo, 50)
    check("清理了重复记录", removed_dup >= 2, f"removed={removed_dup} before={before}")
    check(
        "重复内容只剩一条（隔很久的那条保留）",
        sum(1 for m in after_rows if m["content"] == "同一句") == 2,
        str(after_rows),
    )

    # 31.9 去重指令可用
    dedupe_res = await mock_await_result(
        plugin.companion_dedupe(MockEvent(dup2_umo, "陪伴 去重"))
    )
    check(
        "去重指令有回复",
        dedupe_res is not None and "重复" in dedupe_res[1],
        str(dedupe_res),
    )

    # 31.6 历史里与当前 prompt 相同的那条会被去掉（否则模型会数成两次）
    class _Req6:
        def __init__(self) -> None:
            self.extra_user_content_parts: list[Any] = []
            self.prompt = "我发了一条"
            self.contexts: list[Any] = [
                {"role": "user", "content": "我发了一条"},
                {"role": "assistant", "content": "收到"},
                {"role": "user", "content": "我发了一条"},
            ]

    req6 = _Req6()
    removed6 = plugin._dedupe_request_contexts(req6)
    check(
        "历史里与 prompt 重复的条目被去掉",
        removed6 == 2 and len(req6.contexts) == 1,
        f"removed={removed6} {req6.contexts}",
    )

    # 31.7 prompt 与历史内容不同则不动
    class _Req7:
        def __init__(self) -> None:
            self.extra_user_content_parts: list[Any] = []
            self.prompt = "这是新的问题"
            self.contexts: list[Any] = [
                {"role": "user", "content": "我发了一条"},
                {"role": "assistant", "content": "收到"},
            ]

    req7 = _Req7()
    plugin._dedupe_request_contexts(req7)
    check(
        "prompt 不同时历史保持原样",
        len(req7.contexts) == 2,
        str(req7.contexts),
    )

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
