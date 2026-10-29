"""主动消息的文案生成。

优先使用用户配置的 LLM Provider 生成内容；当没有可用 Provider、调用失败或
返回内容不可用时，退化为内置的模板文案，保证主动消息功能在任何环境下都能工作。
"""

from __future__ import annotations

import random
import re
import time
from typing import Any

from .emotion import EmotionState, label_for, tone_hint

DEFAULT_SYSTEM_PROMPT = (
    "你是一个有性格的 AI 伙伴。你会主动给用户发消息，就像真人发微信一样自然。"
    "请用中文回复，语气口语化、简短（一般 1-3 句，不要长篇大论），"
    "不要使用 Markdown 语法，不要输出括号里的动作描写，也不要以'作为AI'开头。"
)

FALLBACK_TEMPLATES: dict[str, tuple[str, ...]] = {
    "超开心": (
        "今天心情超好，有什么事想和你分享吗？",
        "突然有点想说话了，你在忙吗？",
    ),
    "开心": (
        "刚刚突然想到你，来冒个泡～",
        "今天有什么好事吗？说来听听。",
    ),
    "心情不错": (
        "在吗？随便聊两句也行。",
        "我这边一切都好，你呢？",
    ),
    "平静": (
        "好久没说话了，最近还好吗？",
        "打扰一下，你现在忙不忙？",
    ),
    "兴奋": (
        "来来来，聊聊！",
        "突然精神了，说点有意思的？",
    ),
    "有点闷": (
        "有点无聊……你那边呢？",
        "要不说点什么？",
    ),
    "低落": (
        "今天有点提不起劲……你在吗？",
        "想找人说说话。",
    ),
    "生气": (
        "哼，先不生气了。你最近还好吗？",
        "刚才心情不太好，不过看到你消息好多了。",
    ),
}

GENERIC_TEMPLATES = (
    "在忙什么呢？",
    "突然想到你，冒个泡。",
    "有空吗？想聊两句。",
    "今天过得怎么样？",
)

MAX_LENGTH = 300


def build_context_block(
    recent_messages: list[dict[str, Any]],
    astro_history: list[str] | None = None,
    max_items: int = 12,
    names: dict[str, str] | None = None,
) -> str:
    """把插件记录的近期消息与 AstrBot 会话历史拼成上下文文本。

    names 是「用户 ID -> 最新昵称」的映射，用来把历史消息里的旧昵称统一替换
    成用户现在的昵称（改过昵称后上下文里就不会同时出现新旧两个名字）。
    """
    lines: list[str] = []
    for item in astro_history or []:
        if isinstance(item, str) and item.strip():
            lines.append(item.strip())
    for msg in recent_messages or []:
        role = msg.get("role", "")
        content = str(msg.get("content", "")).strip()
        if not content:
            continue
        sender = str(msg.get("sender", "") or "").strip()
        sender_id = str(msg.get("sender_id", "") or "").strip()
        if names and sender_id and names.get(sender_id):
            sender = names[sender_id]
        if role == "user":
            lines.append(f"{sender or '用户'}: {content}")
        elif role == "bot":
            lines.append(f"我: {content}")
        elif role == "proactive":
            lines.append(f"我(主动): {content}")
        else:
            lines.append(f"{sender or '对方'}: {content}")
    if not lines:
        return ""
    return "\n".join(lines[-max_items:])


def build_prompt(
    context_block: str,
    mood: EmotionState,
    idle_minutes: float,
    is_group: bool,
    group_title: str = "",
    peer_name: str = "",
) -> str:
    """构造发给 LLM 的提示词。"""
    now_str = time.strftime("%Y-%m-%d %H:%M")
    parts = [f"当前时间：{now_str}"]
    if group_title:
        parts.append(f"当前群聊：{group_title}")
    parts.append(
        f"距离上一次对话已经过去了约 {int(idle_minutes)} 分钟，"
        "你可以主动开启一个新话题。"
    )
    parts.append(f"你现在的情绪是「{label_for(mood)}」。{tone_hint(mood)}")
    if peer_name:
        parts.append(
            f"你正在和「{peer_name}」说话。请用「{peer_name}」称呼对方，"
            "不要用「亲爱的」「宝贝」「小朋友」这类泛称，也不要编造别的称呼。"
        )
    if is_group:
        parts.append(
            "这是群聊场景，请注意分寸，不要刷屏，内容要简短自然，"
            "并且不要假装对方正在和你对话。"
        )
    else:
        parts.append(
            "这是一对一的私聊，只有对方在跟你说话，可以比群里更随意一点："
            "像真人找朋友聊天那样自然，可以顺着他之前说过的话接，"
            "也可以关心一下对方最近怎么样，但别追问敏感话题，也别显得查户口。"
        )
    if context_block:
        parts.append("以下是最近的对话记录（可能为空）：\n" + context_block)
    parts.append(
        "请只输出你要发送的那一条消息正文，不要任何解释、标签或引号。"
        "如果此刻并不适合说话，就回复一个空字符串（什么都不说）。"
    )
    return "\n\n".join(parts)


def sanitize(text: str | None) -> str:
    """清洗模型输出，去掉常见的多余包装。"""
    if not text:
        return ""
    cleaned = text.strip()
    cleaned = re.sub(r"^<think>.*?</think>", "", cleaned, flags=re.S)
    cleaned = re.sub(r"<think>.*", "", cleaned, flags=re.S)
    cleaned = cleaned.strip()
    # 去掉模型习惯性添加的引号
    if len(cleaned) >= 2 and cleaned[0] in "\"'“" and cleaned[-1] in "\"'”":
        cleaned = cleaned[1:-1].strip()
    # 去掉 "Assistant:" / "我：" 之类的前缀
    cleaned = re.sub(
        r"^(assistant|assistant:|ai|bot|我|机器人)\s*[:：]\s*", "", cleaned, flags=re.I
    )
    cleaned = cleaned.strip()
    if len(cleaned) > MAX_LENGTH:
        cleaned = cleaned[:MAX_LENGTH].rstrip() + "…"
    return cleaned


def fallback_text(mood: EmotionState, is_group: bool) -> str:
    """在无法调用 LLM 时使用模板文案。"""
    label = label_for(mood)
    pool = FALLBACK_TEMPLATES.get(label) or GENERIC_TEMPLATES
    if is_group and mood.valence < -0.3:
        # 心情不好时在群里不宜刷屏
        pool = GENERIC_TEMPLATES
    return random.choice(pool)


async def generate_text(
    provider: Any,
    prompt: str,
    system_prompt: str,
    mood: EmotionState,
    is_group: bool,
    session_id: str = "",
) -> str:
    """调用 LLM 生成主动消息；失败时返回模板文案。"""
    if provider is not None:
        try:
            resp = await provider.text_chat(
                prompt=prompt,
                system_prompt=system_prompt,
                session_id=session_id or None,
            )
            text = ""
            if resp is not None:
                text = getattr(resp, "completion_text", "") or ""
            text = sanitize(text)
            if text:
                return text
        except Exception:
            # 记录异常但不打断主动消息流程
            pass
    return fallback_text(mood, is_group)
