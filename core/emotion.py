"""动态情绪引擎。

情绪由两个维度构成：
- valence（效价）：-1.0（低落/生气） ~ 1.0（开心/兴奋）
- arousal（唤醒度）：0.0（平静） ~ 1.0（激动）

情绪随时间向基线衰减，并会根据用户输入中的情感词、互动频率、主动消息是否被回应
等事件发生漂移。情绪状态会直接影响主动消息的措辞与触发频率。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

BASE_VALENCE = 0.0
BASE_AROUSAL = 0.3

POSITIVE_WORDS = (
    "喜欢",
    "爱你",
    "谢谢",
    "感谢",
    "哈哈",
    "哈哈哈",
    "嘿嘿",
    "开心",
    "快乐",
    "棒",
    "厉害",
    "真棒",
    "好耶",
    "爱你哦",
    "想你",
    "抱抱",
    "么么",
    "good",
    "great",
    "thanks",
    "love",
    "nice",
)

NEGATIVE_WORDS = (
    "讨厌",
    "烦",
    "闭嘴",
    "滚",
    "笨",
    "讨厌你",
    "生气",
    "别烦我",
    "不想理你",
    "闭嘴吧",
    "无聊",
    "丑",
    "bad",
    "stupid",
    "hate",
    "stop",
)

SAD_WORDS = (
    "难过",
    "伤心",
    "累",
    "疲惫",
    "哭",
    "孤独",
    "低落",
    "郁闷",
    "抑郁",
    "sad",
    "tired",
)


@dataclass
class EmotionState:
    """某个会话的当前情绪状态。"""

    valence: float = BASE_VALENCE
    arousal: float = BASE_AROUSAL
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "valence": round(self.valence, 4),
            "arousal": round(self.arousal, 4),
            "updated_at": self.updated_at,
        }


def clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def decay(
    state: EmotionState,
    now: float | None = None,
    rate: float = 0.15,
    base_valence: float = BASE_VALENCE,
    base_arousal: float = BASE_AROUSAL,
) -> EmotionState:
    """按时间衰减情绪，使其回归基线。返回新的状态（不修改入参）。"""
    now = time.time() if now is None else now
    updated_at = state.updated_at or now
    hours = max(0.0, (now - updated_at) / 3600.0)
    if hours <= 0:
        return EmotionState(state.valence, state.arousal, updated_at)
    factor = math.exp(-rate * hours)
    valence = base_valence + (state.valence - base_valence) * factor
    arousal = base_arousal + (state.arousal - base_arousal) * factor
    return EmotionState(clamp(valence), clamp(arousal, 0.0, 1.0), now)


def analyze_text(text: str) -> tuple[float, float]:
    """从文本中解析情感倾向，返回 (valence 增量, arousal 增量)。"""
    if not text:
        return 0.0, 0.0
    lowered = text.lower()
    valence = 0.0
    arousal = 0.0
    for word in POSITIVE_WORDS:
        if word in lowered:
            valence += 0.12
            arousal += 0.08
    for word in NEGATIVE_WORDS:
        if word in lowered:
            valence -= 0.18
            arousal += 0.14
    for word in SAD_WORDS:
        if word in lowered:
            valence -= 0.1
            arousal -= 0.05
    # 感叹号与短促回复会提高唤醒度
    if "!" in text or "！" in text:
        arousal += 0.05
    if len(text.strip()) <= 4:
        arousal -= 0.02
    return clamp(valence), clamp(arousal, -1.0, 1.0)


def apply_delta(state: EmotionState, dv: float, da: float) -> EmotionState:
    """在当前情绪上叠加增量。"""
    return EmotionState(
        clamp(state.valence + dv),
        clamp(state.arousal + da, 0.0, 1.0),
        state.updated_at or time.time(),
    )


def label_for(state: EmotionState) -> str:
    """根据情绪状态给出可读标签。"""
    v = state.valence
    a = state.arousal
    if v <= -0.45 and a >= 0.55:
        return "生气"
    if v <= -0.45:
        return "低落"
    if v <= -0.15:
        return "有点闷"
    if v >= 0.45 and a >= 0.6:
        return "超开心"
    if v >= 0.45:
        return "开心"
    if v >= 0.15:
        return "心情不错"
    if a >= 0.6:
        return "兴奋"
    return "平静"


def tone_hint(state: EmotionState) -> str:
    """给 LLM 的语气提示，让生成的主动消息符合当前情绪。"""
    v = state.valence
    a = state.arousal
    if v <= -0.45:
        return "情绪低落、有点委屈，说话克制、简短，不要强行打趣。"
    if v <= -0.15:
        return "情绪一般，稍微收敛一些，语气温和但不要过分热情。"
    if v >= 0.45 and a >= 0.6:
        return "非常兴奋、很开心，语气活泼，可以适度撒娇。"
    if v >= 0.45:
        return "心情很好，语气轻快、友好。"
    if a >= 0.6:
        return "情绪比较亢奋，语气活泼、有活力。"
    return "情绪平稳，语气自然、温和。"
