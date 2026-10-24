"""气氛感知（读空气）。

根据一个会话最近的发言，算出「现在这个群是什么气氛」，并给出行为建议：

- 冷清：没什么人说话 → 可以找个轻松话题暖场
- 正常：正常聊天 → 自然接话
- 热闹：聊得很嗨 → 别抢话
- 紧张：有争执/阴阳怪气 → 别参与

判断依据（全部只依赖插件自己采集到的消息记录，不依赖 LLM）：
- 活跃度：最近窗口内的发言频率
- 参与度：不同说话人的数量
- 情绪倾向：正向词 / 负向词的比例
- 冲突度：争执、攻击性措辞的密度
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# 气氛等级
LEVEL_COLD = "冷清"
LEVEL_NORMAL = "正常"
LEVEL_HOT = "热闹"
LEVEL_TENSE = "紧张"

POSITIVE_WORDS = (
    "哈哈",
    "哈哈哈",
    "笑死",
    "草",
    "233",
    "可爱",
    "喜欢",
    "棒",
    "赞",
    "好人",
    "谢谢",
    "晚安",
    "早安",
    "开心",
    "舒服",
    "nice",
    "lol",
)

NEGATIVE_WORDS = (
    "无聊",
    "好丑",
    "垃圾",
    "难看",
    "烦",
    "恶心",
    "滚",
    "别装",
    "无语",
    "没意思",
    "bad",
    "ugly",
)

CONFLICT_WORDS = (
    "吵",
    "打架",
    "举报",
    "退群",
    "封号",
    "屏蔽",
    "拉黑",
    "闭嘴",
    "滚蛋",
    "傻逼",
    "煞笔",
    "你妈",
    "妈的",
    "有病",
    "别他妈",
    "笑死你",
    "傻",
)

DEFAULT_WINDOW_MINUTES = 30
COLD_RATE = 0.12  # 每分钟发言低于此值算冷清
HOT_RATE = 0.8  # 每分钟发言高于此值算热闹
CONFLICT_RATIO = 0.15  # 冲突词占比高于此值算紧张


@dataclass
class Atmosphere:
    """某个会话当前的气氛快照。"""

    level: str = LEVEL_NORMAL
    score: float = 0.0  # 活跃度 0~1
    positivity: float = 0.0  # 情绪倾向 -1~1
    conflict: float = 0.0  # 冲突度 0~1
    participants: int = 0
    message_count: int = 0
    advice: str = ""
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "score": round(self.score, 3),
            "positivity": round(self.positivity, 3),
            "conflict": round(self.conflict, 3),
            "participants": self.participants,
            "message_count": self.message_count,
            "advice": self.advice,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> Atmosphere:
        data = data or {}
        return cls(
            level=str(data.get("level") or LEVEL_NORMAL),
            score=float(data.get("score") or 0.0),
            positivity=float(data.get("positivity") or 0.0),
            conflict=float(data.get("conflict") or 0.0),
            participants=int(data.get("participants") or 0),
            message_count=int(data.get("message_count") or 0),
            advice=str(data.get("advice") or ""),
            updated_at=float(data.get("updated_at") or 0.0),
        )


def _count_hits(text: str, words: tuple[str, ...]) -> int:
    lowered = text.lower()
    return sum(1 for word in words if word in lowered)


def analyze(
    messages: list[dict],
    now: float | None = None,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    cold_rate: float = COLD_RATE,
    hot_rate: float = HOT_RATE,
    conflict_ratio: float = CONFLICT_RATIO,
) -> Atmosphere:
    """根据最近的发言算出气氛。

    messages 里每项需要包含 content / sender / ts 字段（插件自己的消息表结构）。
    """
    now = time.time() if now is None else now
    window = max(1, int(window_minutes))
    recent: list[dict] = []
    for msg in messages:
        try:
            ts = float(msg.get("ts") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts and (now - ts) > window * 60:
            continue
        if str(msg.get("role")) == "bot":
            # 机器人自己的发言不计入群里的气氛
            continue
        recent.append(msg)

    if not recent:
        return Atmosphere(
            level=LEVEL_COLD,
            score=0.0,
            positivity=0.0,
            conflict=0.0,
            participants=0,
            message_count=0,
            advice="群里安静得像个空城，可以考虑找个轻松话题暖场。",
            updated_at=now,
        )

    texts = [str(m.get("content") or "") for m in recent]
    senders = {
        str(m.get("sender") or "").strip()
        for m in recent
        if str(m.get("sender") or "").strip()
    }
    timestamps = [float(m.get("ts") or 0.0) for m in recent if m.get("ts")]
    span_minutes = 1.0
    if len(timestamps) >= 2:
        span = max(timestamps) - min(timestamps)
        span_minutes = max(1.0, span / 60.0)
    count = len(recent)
    rate = count / span_minutes
    score = min(1.0, rate / max(0.1, hot_rate))

    joined = " ".join(texts)
    pos_hits = _count_hits(joined, POSITIVE_WORDS)
    neg_hits = _count_hits(joined, NEGATIVE_WORDS)
    total_sentiment = pos_hits + neg_hits
    positivity = (pos_hits - neg_hits) / total_sentiment if total_sentiment else 0.0
    conflict_hits = _count_hits(joined, CONFLICT_WORDS)
    conflict = min(1.0, conflict_hits / max(1, count))

    if conflict > conflict_ratio:
        level = LEVEL_TENSE
        advice = (
            "群里气氛有点僵（有人在互相冒犯），这时候千万别插嘴，更不要跟着阴阳怪气。"
        )
    elif rate >= hot_rate and len(senders) >= 3:
        level = LEVEL_HOT
        advice = "群里正聊得起劲，大家都有话讲。这时候别抢话，最多轻轻接一句就收。"
    elif rate <= cold_rate or count < 2:
        level = LEVEL_COLD
        advice = (
            "群里挺安静的，没什么人接话。可以找个轻松的话题暖场，"
            "但别硬聊，也别自说自话。"
        )
    else:
        level = LEVEL_NORMAL
        advice = "群里气氛正常，自然接话就好，不用刻意。"

    return Atmosphere(
        level=level,
        score=score,
        positivity=positivity,
        conflict=conflict,
        participants=len(senders),
        message_count=count,
        advice=advice,
        updated_at=now,
    )


def probability_factor(atmosphere: Atmosphere, boost: float = 1.5) -> float:
    """根据气氛返回主动回复概率的乘数。"""
    if atmosphere.level == LEVEL_TENSE:
        return 0.0
    if atmosphere.level == LEVEL_HOT:
        return 0.3
    if atmosphere.level == LEVEL_COLD:
        return max(0.0, boost)
    return 1.0


def allow_proactive_message(atmosphere: Atmosphere) -> bool:
    """定时主动搭话是否该放行。"""
    return atmosphere.level not in (LEVEL_TENSE, LEVEL_HOT)
