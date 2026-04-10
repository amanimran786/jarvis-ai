#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QBrush, QGuiApplication, QIcon, QPainter, QPen, QPixmap, QRadialGradient


ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
ICONSET = ASSETS / "icon.iconset"
ICON_1024 = ASSETS / "icon_1024.png"
ICON_ICNS = ASSETS / "jarvis.icns"


def render_orb_icon(size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    center = size / 2
    outer = size * 0.42
    inner = size * 0.18

    bg = QRadialGradient(center, center, outer)
    bg.setColorAt(0.0, QColor(9, 40, 60, 255))
    bg.setColorAt(0.55, QColor(2, 18, 28, 245))
    bg.setColorAt(1.0, QColor(1, 8, 14, 235))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(bg))
    painter.drawEllipse(int(center - outer), int(center - outer), int(outer * 2), int(outer * 2))

    glow_pen = QPen(QColor("#29d9ff"), max(8, size // 40))
    glow_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(glow_pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(
        int(center - outer * 0.72),
        int(center - outer * 0.72),
        int(outer * 1.44),
        int(outer * 1.44),
    )

    core = QRadialGradient(center, center, inner)
    core.setColorAt(0.0, QColor(255, 255, 255, 250))
    core.setColorAt(0.35, QColor(80, 225, 255, 240))
    core.setColorAt(1.0, QColor(0, 120, 180, 0))
    painter.setBrush(QBrush(core))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(int(center - inner), int(center - inner), int(inner * 2), int(inner * 2))

    painter.end()
    return pixmap


def build_iconset() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    ICONSET.mkdir(parents=True, exist_ok=True)

    sizes = {
        "icon_16x16.png": 16,
        "icon_16x16@2x.png": 32,
        "icon_32x32.png": 32,
        "icon_32x32@2x.png": 64,
        "icon_128x128.png": 128,
        "icon_128x128@2x.png": 256,
        "icon_256x256.png": 256,
        "icon_256x256@2x.png": 512,
        "icon_512x512.png": 512,
        "icon_512x512@2x.png": 1024,
    }

    for name, size in sizes.items():
        render_orb_icon(size).save(str(ICONSET / name))

    render_orb_icon(1024).save(str(ICON_1024))


def build_icns() -> None:
    subprocess.run(
        ["iconutil", "-c", "icns", str(ICONSET), "-o", str(ICON_ICNS)],
        check=True,
    )


def main() -> int:
    app = QGuiApplication(sys.argv)
    app.setWindowIcon(QIcon())
    build_iconset()
    build_icns()
    print(f"Built {ICON_ICNS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
