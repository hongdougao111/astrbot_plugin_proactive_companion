"""生成插件 logo.png（256x256，纯标准库实现）。"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

SIZE = 256


def rounded_rect(
    x: float, y: float, x0: float, y0: float, x1: float, y1: float, r: float
) -> bool:
    if x < x0 or x > x1 or y < y0 or y > y1:
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def in_triangle(px: float, py: float, tri: list[tuple[float, float]]) -> bool:
    (ax, ay), (bx, by), (cx, cy) = tri

    def sign(p1, p2, p3):
        return (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])

    d1 = sign((px, py), (ax, ay), (bx, by))
    d2 = sign((px, py), (bx, by), (cx, cy))
    d3 = sign((px, py), (cx, cy), (ax, ay))
    has_neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    has_pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (has_neg and has_pos)


def in_heart(px: float, py: float, cx: float, cy: float, scale: float) -> bool:
    x = (px - cx) / scale
    y = (py - cy) / scale
    val = (x * x + y * y - 1.0) ** 3 - x * x * (y**3)
    return val <= 0


def lerp(
    a: tuple[int, int, int], b: tuple[int, int, int], t: float
) -> tuple[int, int, int]:
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


def build_pixels() -> list[list[tuple[int, int, int, int]]]:
    top = (79, 110, 247)
    bottom = (139, 92, 246)
    rows: list[list[tuple[int, int, int, int]]] = []

    for py in range(SIZE):
        row: list[tuple[int, int, int, int]] = []
        t = py / (SIZE - 1)
        bg = lerp(top, bottom, t)
        for px in range(SIZE):
            # 背景圆角
            if not rounded_rect(px + 0.5, py + 0.5, 6, 6, SIZE - 6, SIZE - 6, 54):
                row.append((0, 0, 0, 0))
                continue

            color = bg
            alpha = 255

            # 气泡
            bubble = rounded_rect(px + 0.5, py + 0.5, 46, 56, 210, 162, 34)
            # 气泡尾巴
            tail = in_triangle(
                px + 0.5,
                py + 0.5,
                [(74, 150), (74, 190), (116, 154)],
            )
            if bubble or tail:
                color = (255, 255, 255)
                alpha = 255

            # 气泡里的爱心
            if bubble and in_heart(px + 0.5, py + 0.5, 128, 104, 40):
                color = (244, 84, 106)

            row.append((color[0], color[1], color[2], alpha))
        rows.append(row)
    return rows


def write_png(path: Path, rows: list[list[tuple[int, int, int, int]]]) -> None:
    raw = bytearray()
    for row in rows:
        raw.append(0)  # filter type 0
        for r, g, b, a in row:
            raw.extend((r, g, b, a))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


if __name__ == "__main__":
    target = Path(__file__).resolve().parent.parent / "logo.png"
    write_png(target, build_pixels())
    print(f"written: {target} ({target.stat().st_size} bytes)")
