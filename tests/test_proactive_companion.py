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

    api_event.filter = _Filter()  # type: ignore[attr-defined]
    # 真实 API 中 filter 是模块，EventMessageType 等从模块导出
    api_event.filter.EventMessageType = EventMessageType
    api_event.filter.PermissionType = type("PermissionType", (), {})
    api_event.filter.PlatformAdapterType = type("PlatformAdapterType", (), {})

    api_msg = types.ModuleType("astrbot.api.message_components")
    api_msg.Record = MockRecord  # type: ignore[attr-defined]

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
