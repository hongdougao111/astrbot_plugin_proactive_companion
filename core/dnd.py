"""免打扰时段（DND）处理。

支持 `HH:MM-HH:MM` 形式的时段列表，例如：

- `["23:00-07:30"]`  普通时段
- `["23:00-07:30", "12:30-13:30"]`  多个时段
- `["00:00-24:00"]`  全天免打扰

跨越午夜（例如 23:00-07:00）会被正确处理。
"""

from __future__ import annotations

import datetime as _dt

DEFAULT_RANGES: list[str] = []


def _parse_time(value: str) -> tuple[int, int] | None:
    """把 `HH:MM` 解析为 (hour, minute)，非法格式返回 None。"""
    value = (value or "").strip()
    if not value:
        return None
    parts = value.split(":")
    if len(parts) != 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 24) or not (0 <= minute <= 59):
        return None
    if hour == 24 and minute != 0:
        return None
    return hour, minute


def _minutes(t: tuple[int, int] | None) -> int:
    if t is None:
        return 0
    return t[0] * 60 + t[1]


def parse_ranges(ranges: list[str] | None) -> list[tuple[int, int]]:
    """解析时段列表为当天的分钟区间列表 [(start, end), ...]。"""
    parsed: list[tuple[int, int]] = []
    for raw in ranges or []:
        if not isinstance(raw, str) or "-" not in raw:
            continue
        left, _, right = raw.partition("-")
        start = _parse_time(left)
        end = _parse_time(right)
        if start is None or end is None:
            continue
        s = _minutes(start)
        e = _minutes(end)
        if s == e:
            # 起点与终点相同视为空区间
            continue
        parsed.append((s, e))
    return parsed


def in_quiet_hours(
    now: _dt.datetime | None = None, ranges: list[str] | None = None
) -> bool:
    """判断当前时间是否落在免打扰时段内。"""
    parsed = parse_ranges(ranges)
    if not parsed:
        return False
    now = now or _dt.datetime.now()
    current = now.hour * 60 + now.minute
    for start, end in parsed:
        if start < end:
            if start <= current < end:
                return True
        else:
            # 跨午夜
            if current >= start or current < end:
                return True
    return False


def next_active_time(
    now: _dt.datetime | None = None, ranges: list[str] | None = None
) -> _dt.datetime | None:
    """返回下一个不在免打扰时段的时刻；没有任何时段时返回 None。"""
    parsed = parse_ranges(ranges)
    if not parsed:
        return None
    now = now or _dt.datetime.now()
    candidate = now.replace(second=0, microsecond=0)
    for _ in range(24 * 60 + 2):
        if not in_quiet_hours(candidate, ranges):
            return candidate
        candidate += _dt.timedelta(minutes=1)
    return None
