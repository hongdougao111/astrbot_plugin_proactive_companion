"""好感度与人物画像。

好感度（affection）刻画 Bot 与某个用户之间的**长期关系**：

- 每次互动加分，用户态度好加分多、负面情绪扣分
- 主动回复、认真回应都会加分
- 长时间不互动会缓慢衰减（关系会变淡）

人物画像（profile）汇总一个用户在某个会话里的可读档案：昵称、ID、好感等级、
消息量、首次/最近出现、最近发言，以及可由用户手动编辑的备注与标签。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# 好感等级（从低到高）
LEVELS: tuple[tuple[float, str], ...] = (
    (0.0, "陌生"),
    (10.0, "点头之交"),
    (30.0, "熟人"),
    (60.0, "亲近"),
    (85.0, "挚友"),
)

AFFECTION_MIN = 0.0
AFFECTION_MAX = 100.0

# 各类事件的加减分
DELTA_USER_MESSAGE = 0.6
DELTA_BOT_REPLY = 1.2
DELTA_PROACTIVE_REPLY = 1.8
DELTA_POSITIVE = 2.0
DELTA_NEGATIVE = 3.0
DELTA_COMMAND = -0.2
# 每天无互动的衰减
DECAY_PER_DAY = 1.5


def clamp(value: float) -> float:
    return max(AFFECTION_MIN, min(AFFECTION_MAX, value))


def level_of(score: float) -> str:
    """按好感分返回关系等级。"""
    label = LEVELS[0][1]
    for threshold, name in LEVELS:
        if score >= threshold:
            label = name
    return label


def level_index(score: float) -> int:
    """关系等级序号（0 = 陌生）。"""
    idx = 0
    for i, (threshold, _name) in enumerate(LEVELS):
        if score >= threshold:
            idx = i
    return idx


@dataclass
class Affection:
    """某个用户与 Bot 的好感状态。"""

    score: float = 0.0
    updated_at: float = field(default_factory=time.time)

    @property
    def level(self) -> str:
        return level_of(self.score)

    @property
    def level_index(self) -> int:
        return level_index(self.score)

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 2),
            "level": self.level,
            "level_index": self.level_index,
            "updated_at": self.updated_at,
        }


def decayed(score: float, updated_at: float, now: float | None = None) -> float:
    """按无互动时长衰减好感度。"""
    now = time.time() if now is None else now
    if not updated_at:
        return clamp(score)
    days = max(0.0, (now - updated_at) / 86400.0)
    if days <= 0:
        return clamp(score)
    return clamp(score - days * DECAY_PER_DAY)


def apply_event(
    score: float,
    event: str,
    positive: bool = False,
    negative: bool = False,
) -> float:
    """在好感分上叠加一次事件。"""
    value = score
    if event == "user_message":
        value += DELTA_USER_MESSAGE
    elif event == "bot_reply":
        value += DELTA_BOT_REPLY
    elif event == "proactive_reply":
        value += DELTA_PROACTIVE_REPLY
    elif event == "command":
        value += DELTA_COMMAND
    if positive:
        value += DELTA_POSITIVE
    if negative:
        value -= DELTA_NEGATIVE
    return clamp(value)


def probability_factor(score: float) -> float:
    """好感越高，越愿意主动搭话（0.8 ~ 1.4）。"""
    ratio = clamp(score) / AFFECTION_MAX
    return 0.8 + ratio * 0.6


def relationship_hint(score: float) -> str:
    """给模型的一句话关系提示。"""
    name = level_of(score)
    if name == "陌生":
        return "你和 TA 还不太熟，说话要自然、别太热情。"
    if name == "点头之交":
        return "你和 TA 算是刚认识不久，保持礼貌自然。"
    if name == "熟人":
        return "你和 TA 已经挺熟了，可以随意一点。"
    if name == "亲近":
        return "你和 TA 关系很亲近，说话可以更放松、更关心对方。"
    return "你和 TA 是很要好的朋友，可以像老朋友一样自然地聊。"
