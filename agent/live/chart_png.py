"""Мінімальний рендер PNG-графіків на чистому Python (без matplotlib — працює в Termux).

Лінії, горизонтальні рівні, маркери-трикутники; темна тема. Підписи — у тексті повідомлення Telegram.
"""

from __future__ import annotations

import struct
import zlib

BG = (20, 22, 28)
GRID = (42, 46, 56)
COLORS = {"eth": (247, 147, 26), "pf": (77, 163, 255), "buy": (52, 199, 89), "sell": (255, 69, 58),
          "stop": (255, 69, 58), "entry": (52, 199, 89), "white": (230, 232, 238), "grey": (120, 126, 140)}


class Canvas:
    def __init__(self, w: int = 900, h: int = 480):
        self.w, self.h = w, h
        self.px = bytearray(bytes(BG) * (w * h))

    def set(self, x: int, y: int, c) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 3
            self.px[i:i + 3] = bytes(c)

    def dot(self, x: int, y: int, c, r: int = 1) -> None:
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                if dx * dx + dy * dy <= r * r + 1:
                    self.set(x + dx, y + dy, c)

    def line(self, x0, y0, x1, y1, c, width: int = 2, dash: int = 0) -> None:
        x0, y0, x1, y1 = int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        err, n = dx + dy, 0
        r = max(0, width // 2)
        while True:
            if not dash or (n // dash) % 2 == 0:
                self.dot(x0, y0, c, r) if r else self.set(x0, y0, c)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy
            n += 1

    def tri(self, x: int, y: int, c, up: bool = True, s: int = 7) -> None:
        for k in range(s + 1):
            yy = y - s + k if up else y + s - k
            for xx in range(x - k, x + k + 1):
                self.set(xx, yy + (s // 2 if up else -(s // 2)), c)

    def png(self) -> bytes:
        raw = b"".join(b"\x00" + bytes(self.px[y * self.w * 3:(y + 1) * self.w * 3]) for y in range(self.h))

        def chunk(t, d):
            return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def chart(series: list[tuple[list[tuple[float, float]], tuple]], hlines=(), markers=(), w: int = 900, h: int = 480,
          pad: int = 24) -> bytes:
    """series: [(точки [(x, y)], колір)], hlines: [(y, колір, пунктир)], markers: [(x, y, колір, вгору?)]."""
    cv = Canvas(w, h)
    xs = [p[0] for s, _ in series for p in s] or [0, 1]
    ys = [p[1] for s, _ in series for p in s] + [y for y, *_ in hlines] + [m[1] for m in markers] or [0, 1]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if x1 == x0:
        x1 = x0 + 1
    if y1 == y0:
        y1 = y0 + 1
    m = (y1 - y0) * 0.06
    y0, y1 = y0 - m, y1 + m
    X = lambda x: pad + (x - x0) / (x1 - x0) * (w - 2 * pad)
    Y = lambda y: h - pad - (y - y0) / (y1 - y0) * (h - 2 * pad)
    for i in range(5):                                        # сітка
        yy = pad + i * (h - 2 * pad) / 4
        cv.line(pad, yy, w - pad, yy, GRID, 1)
    for y, c, dash in hlines:
        cv.line(pad, Y(y), w - pad, Y(y), c, 2, 8 if dash else 0)
    for pts, c in series:
        for (a, b), (cx, cy) in zip(pts, pts[1:]):
            cv.line(X(a), Y(b), X(cx), Y(cy), c, 3)
        if pts:
            cv.dot(int(X(pts[-1][0])), int(Y(pts[-1][1])), c, 5)
    for x, y, c, up in markers:
        if x0 <= x <= x1:
            cv.tri(int(X(x)), int(Y(y)), c, up)
    return cv.png()


def downsample(pts: list, n: int = 450) -> list:
    if len(pts) <= n:
        return pts
    step = len(pts) / n
    return [pts[int(i * step)] for i in range(n)] + [pts[-1]]
