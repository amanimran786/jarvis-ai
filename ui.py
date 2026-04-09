import sys
import re
import threading
import os
import math
import random
import subprocess
import shutil
from datetime import datetime
import learner
import model_router
import self_improve as si
import hotkeys
import meeting_listener
import api
import agents
import evals
import conversation_context as ctx
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QTextEdit, QLineEdit, QLabel, QFileDialog, QInputDialog,
    QScrollArea, QFrame, QSizePolicy, QGraphicsDropShadowEffect
)
from PyQt6.QtCore import (
    Qt, QObject, QThread, pyqtSignal, QTimer, QSize, QPropertyAnimation,
    QRect, QPoint, QEasingCurve, QRectF
)
from PyQt6.QtGui import (
    QFont, QColor, QPalette, QIcon, QTextCursor, QKeyEvent,
    QPainter, QPen, QBrush, QLinearGradient, QRadialGradient,
    QPainterPath, QFontDatabase, QCursor, QPixmap
)

from router import route_stream, set_timer_callback
from voice import speak, speak_stream, listen, wait_for_wake_word
import memory as mem
import briefing
import tools
import google_services as gs
import terminal
import stealth
import hardware as hw
import browser
import overlay as _overlay_mod
import call_privacy

try:
    import meeting_controller as _meeting_controller_mod
except Exception:
    _meeting_controller_mod = None

try:
    import device_panel as _device_panel_mod
except Exception:
    _device_panel_mod = None

try:
    import bridge as _bridge_mod
except Exception:
    _bridge_mod = None

try:
    from Foundation import NSBundle, NSProcessInfo
    from AppKit import NSApplication
except Exception:
    NSBundle = None
    NSProcessInfo = None
    NSApplication = None

# ── Color palette ──────────────────────────────────────────────────────────────
C_BG        = "#020A10"       # deep space black
C_BG2       = "#030D14"       # slightly lighter panel
C_CYAN      = "#00D4FF"       # primary HUD cyan
C_CYAN_DIM  = "#0099BB"       # dimmed cyan
C_CYAN_GLOW = "#00AADD"
C_BLUE      = "#0A3A5C"       # dark blue panel
C_BORDER    = "#0D4F70"       # panel borders
C_TEXT      = "#A8E6FF"       # readable text
C_TEXT_DIM  = "#4A8FA8"       # subdued text
C_ORANGE    = "#FF6B00"       # warning / user accent
C_GREEN     = "#00FF88"       # online / status green
C_PANEL     = "#021018"       # message panel bg
C_WHITE_DIM = "#D8F6FF"       # highlight text
C_WARNING   = "#FFAA00"       # processing state

END_CONVERSATION = {"that's all", "that's it", "done", "thank you", "thanks", "stop listening"}
QUIT_PHRASES = {"quit", "exit", "goodbye", "bye", "shut down"}


# ── Glow helper ────────────────────────────────────────────────────────────────

def _glow(widget, color=C_CYAN, radius=12):
    fx = QGraphicsDropShadowEffect()
    fx.setBlurRadius(radius)
    fx.setColor(QColor(color))
    fx.setOffset(0, 0)
    widget.setGraphicsEffect(fx)
    return fx


def _glass_panel_css(border=C_BORDER, fill="rgba(2, 16, 24, 210)", radius=10):
    return f"""
        background-color: {fill};
        border: 1px solid {border};
        border-radius: {radius}px;
    """


def _call_optional(module, names, *args, **kwargs):
    if module is None:
        return False, None
    for name in names:
        fn = getattr(module, name, None)
        if not callable(fn):
            continue
        try:
            return True, fn(*args, **kwargs)
        except TypeError:
            try:
                return True, fn()
            except TypeError:
                continue
    return False, None


def _meeting_is_running() -> bool:
    handled, value = _call_optional(_meeting_controller_mod, ("is_running", "running"))
    if handled:
        return bool(value)
    return meeting_listener.is_running()


def _meeting_start(on_transcript=None, on_suggestion=None) -> str:
    handled, value = _call_optional(
        _meeting_controller_mod,
        ("start_listening", "start"),
        on_transcript=on_transcript,
        on_suggestion=on_suggestion,
    )
    if handled:
        return value if isinstance(value, str) else str(value or "")
    return meeting_listener.start(
        on_transcript=on_transcript,
        on_suggestion=on_suggestion,
    )


def _meeting_stop() -> str:
    handled, value = _call_optional(_meeting_controller_mod, ("stop_listening", "stop"))
    if handled:
        return value if isinstance(value, str) else str(value or "")
    return meeting_listener.stop()


def _meeting_status_snapshot() -> dict:
    handled, value = _call_optional(
        _meeting_controller_mod,
        ("status_snapshot", "refresh_status", "snapshot"),
        force_refresh=True,
    )
    if handled and isinstance(value, dict):
        return value
    return meeting_listener.status_snapshot()


def _live_listener_snapshot(snapshot: dict) -> dict:
    listener = snapshot.get("listener")
    if isinstance(listener, dict):
        return listener
    return snapshot


def _force_text_widget_update(widget, text: str):
    """
    Force Qt to repaint even when the visible text is identical to the current value.
    """
    current = None
    if hasattr(widget, "toPlainText"):
        try:
            current = widget.toPlainText()
        except Exception:
            current = None
    elif hasattr(widget, "text"):
        try:
            current = widget.text()
        except Exception:
            current = None

    if current == text:
        try:
            widget.clear()
        except Exception:
            pass
    if hasattr(widget, "setPlainText"):
        widget.setPlainText(text)
    else:
        widget.setText(text)


class LiveUpdateBridge(QObject):
    transcript = pyqtSignal(str)
    suggestion = pyqtSignal(str)


def _trace_ui_event(window, surface: str, event: str, text: str = "", **fields):
    snippet = text.strip().replace("\n", " ")
    if len(snippet) > 160:
        snippet = snippet[:157].rstrip() + "..."
    extras = " ".join(f"{key}={value}" for key, value in fields.items() if value not in (None, ""))
    visible = int(bool(getattr(window, "isVisible", lambda: False)()))
    tray = int(bool(getattr(window, "suggest_panel", None) and window.suggest_panel.isVisible()))
    stamp = datetime.now().strftime("%H:%M:%S")
    line = f"[jarvis-ui] {stamp} window={window.__class__.__name__} surface={surface} event={event} visible={visible} tray={tray}"
    if snippet:
        line += f" text={snippet}"
    if extras:
        line += f" {extras}"
    print(line, file=sys.stderr, flush=True)


def _device_refresh_snapshot(timeout: float = 1.2) -> dict:
    handled, value = _call_optional(
        _device_panel_mod,
        ("refresh_devices", "refresh_nearby_devices", "refresh"),
        timeout=timeout,
    )
    if handled and isinstance(value, dict):
        return value
    return hw.discover_nearby(timeout=timeout)


def _device_open_settings(target: str) -> str:
    handled, value = _call_optional(
        _device_panel_mod,
        ("open_device_settings", "open_system_settings", "open_settings"),
        target,
    )
    if handled and isinstance(value, str):
        return value
    return hw.open_system_settings(target)


def _bridge_snapshot_data() -> dict:
    handled, value = _call_optional(
        _bridge_mod,
        ("refresh_bridge", "bridge_status", "status_snapshot", "status"),
    )
    if handled and isinstance(value, dict):
        return value
    return hw.bridge_status(api_host=api.get_host(), api_port=api.get_port())


def _device_copy_bridge_url() -> str | None:
    handled, value = _call_optional(
        _device_panel_mod,
        ("copy_bridge_url", "copy_bridge_link", "copy_bridge_uri"),
    )
    if handled:
        return value if isinstance(value, str) else None
    return None


def _device_copy_current_page_url() -> str | None:
    handled, value = _call_optional(
        _device_panel_mod,
        ("copy_current_page_url", "copy_current_url", "copy_page_url"),
    )
    if handled:
        return value if isinstance(value, str) else None
    return None


def _build_runtime_app_icon(size: int = 512) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    center = size / 2
    outer = size * 0.42
    inner = size * 0.18

    bg = QRadialGradient(center, center, outer)
    bg.setColorAt(0.0, QColor(9, 40, 60, 255))
    bg.setColorAt(0.55, QColor(2, 18, 28, 245))
    bg.setColorAt(1.0, QColor(1, 8, 14, 235))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(bg))
    p.drawEllipse(int(center - outer), int(center - outer), int(outer * 2), int(outer * 2))

    glow_pen = QPen(QColor(C_CYAN), max(8, size // 40))
    glow_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(glow_pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(int(center - outer * 0.72), int(center - outer * 0.72), int(outer * 1.44), int(outer * 1.44))

    core = QRadialGradient(center, center, inner)
    core.setColorAt(0.0, QColor(255, 255, 255, 250))
    core.setColorAt(0.35, QColor(80, 225, 255, 240))
    core.setColorAt(1.0, QColor(0, 120, 180, 0))
    p.setBrush(QBrush(core))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(int(center - inner), int(center - inner), int(inner * 2), int(inner * 2))

    p.end()
    return QIcon(pixmap)


def _apply_macos_identity(app: QApplication, icon: QIcon):
    app.setApplicationName("Jarvis")
    if hasattr(app, "setApplicationDisplayName"):
        app.setApplicationDisplayName("Jarvis")
    if hasattr(app, "setDesktopFileName"):
        app.setDesktopFileName("Jarvis")
    app.setWindowIcon(icon)
    QApplication.setWindowIcon(icon)

    if NSProcessInfo is not None:
        try:
            NSProcessInfo.processInfo().setProcessName_("Jarvis")
        except Exception:
            pass

    if NSBundle is not None:
        try:
            info = NSBundle.mainBundle().infoDictionary()
            if info is not None:
                info["CFBundleName"] = "Jarvis"
                info["CFBundleDisplayName"] = "Jarvis"
                info["CFBundleExecutable"] = "Jarvis"
            localized = NSBundle.mainBundle().localizedInfoDictionary()
            if localized is not None:
                localized["CFBundleName"] = "Jarvis"
                localized["CFBundleDisplayName"] = "Jarvis"
        except Exception:
            pass

    if NSApplication is not None:
        try:
            ns_app = NSApplication.sharedApplication()
            # Promote the bundled launch into a normal foreground Mac app so the
            # Dock and menu bar use the app identity instead of utility-window behavior.
            ns_app.setActivationPolicy_(0)
        except Exception:
            pass


def _activate_macos_app(window: QMainWindow):
    if NSApplication is not None:
        try:
            ns_app = NSApplication.sharedApplication()
            ns_app.activateIgnoringOtherApps_(True)
        except Exception:
            pass
    try:
        window.raise_()
        window.activateWindow()
    except Exception:
        pass

# ── Arc Reactor Widget ─────────────────────────────────────────────────────────

class ArcReactor(QWidget):
    """Animated arc reactor decoration drawn with QPainter."""

    def __init__(self, size=60, parent=None):
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._angle = 0
        self._pulse = 0.0
        self._pulse_dir = 1
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(30)

    def _tick(self):
        self._angle = (self._angle + 2) % 360
        self._pulse += 0.04 * self._pulse_dir
        if self._pulse >= 1.0:
            self._pulse_dir = -1
        elif self._pulse <= 0.0:
            self._pulse_dir = 1
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = self._size / 2, self._size / 2
        r = self._size / 2 - 3

        # Outer glow ring
        glow_alpha = int(60 + 80 * self._pulse)
        pen = QPen(QColor(0, 212, 255, glow_alpha), 3)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QRectF(3, 3, self._size - 6, self._size - 6))

        # Rotating arc segments
        p.save()
        p.translate(cx, cy)
        p.rotate(self._angle)
        arc_pen = QPen(QColor(C_CYAN), 2)
        arc_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(arc_pen)
        p.drawArc(QRectF(-r + 4, -r + 4, (r - 4) * 2, (r - 4) * 2), 0 * 16, 80 * 16)
        p.drawArc(QRectF(-r + 4, -r + 4, (r - 4) * 2, (r - 4) * 2), 120 * 16, 80 * 16)
        p.drawArc(QRectF(-r + 4, -r + 4, (r - 4) * 2, (r - 4) * 2), 240 * 16, 80 * 16)
        p.restore()

        # Counter-rotating inner arc
        p.save()
        p.translate(cx, cy)
        p.rotate(-self._angle * 1.5)
        inner_r = r * 0.55
        dim_pen = QPen(QColor(C_CYAN_DIM), 1)
        p.setPen(dim_pen)
        p.drawArc(QRectF(-inner_r, -inner_r, inner_r * 2, inner_r * 2), 30 * 16, 60 * 16)
        p.drawArc(QRectF(-inner_r, -inner_r, inner_r * 2, inner_r * 2), 150 * 16, 60 * 16)
        p.drawArc(QRectF(-inner_r, -inner_r, inner_r * 2, inner_r * 2), 270 * 16, 60 * 16)
        p.restore()

        # Center dot with pulse
        core_r = 5 + 2 * self._pulse
        grad = QRadialGradient(cx, cy, core_r)
        grad.setColorAt(0, QColor(255, 255, 255, 240))
        grad.setColorAt(0.4, QColor(0, 212, 255, 200))
        grad.setColorAt(1, QColor(0, 100, 180, 0))
        p.setBrush(QBrush(grad))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QRectF(cx - core_r, cy - core_r, core_r * 2, core_r * 2))
        p.end()


# ── Jarvis Orb ────────────────────────────────────────────────────────────────

class JarvisOrb(QWidget):
    """
    Central animated orb — the face of Jarvis.

    States:
      idle      — slow breathing pulse, gently rotating rings
      listening — rings pulse inward, brighter core
      speaking  — fast wave ripples, waveform bars, intense glow
    """

    STATE_IDLE      = "idle"
    STATE_LISTENING = "listening"
    STATE_SPEAKING  = "speaking"

    def __init__(self, size=260, parent=None):
        super().__init__(parent)
        self._sz     = size
        self._r      = size / 2 - 6
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # Animation state
        self._state      = self.STATE_IDLE
        self._tick_n     = 0
        self._pulse      = 0.0
        self._pulse_dir  = 1
        self._ring_angle = 0.0          # outer ring rotation
        self._ring2_ang  = 0.0          # inner ring rotation
        self._wave_phase = 0.0          # wave offset for speaking ripples

        # Waveform bars (32 bars around equator)
        self._bars       = [0.0] * 32
        self._bar_target = [0.0] * 32

        # Timer — 50fps
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(20)

    # ── Public state control ──────────────────────────────────────────────────

    def set_state(self, state: str):
        self._state = state

    # ── Animation tick ────────────────────────────────────────────────────────

    def _tick(self):
        self._tick_n += 1
        spd = {"idle": 1, "listening": 1.8, "speaking": 3.0}.get(self._state, 1)

        # Pulse (breathing)
        self._pulse += 0.025 * spd * self._pulse_dir
        if self._pulse >= 1.0: self._pulse_dir = -1
        elif self._pulse <= 0.0: self._pulse_dir = 1

        # Ring rotation
        self._ring_angle  = (self._ring_angle + 0.6 * spd) % 360
        self._ring2_ang   = (self._ring2_ang  - 0.9 * spd) % 360
        self._wave_phase += 0.08 * spd

        # Waveform bars — smooth towards random targets when speaking
        if self._state == self.STATE_SPEAKING:
            if self._tick_n % 3 == 0:
                for i in range(len(self._bar_target)):
                    self._bar_target[i] = random.uniform(0.2, 1.0)
        else:
            for i in range(len(self._bar_target)):
                self._bar_target[i] = 0.05 + 0.1 * abs(math.sin(
                    self._wave_phase + i * 0.3
                ))

        for i in range(len(self._bars)):
            self._bars[i] += (self._bar_target[i] - self._bars[i]) * 0.25

        self.update()

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        cx = cy = self._sz / 2
        r  = self._r
        pulse = self._pulse

        # ── 1. Ambient outer glow ─────────────────────────────────────────
        glow_alpha = int(25 + 20 * pulse)
        for glow_r in [r + 18, r + 10, r + 4]:
            g = QRadialGradient(cx, cy, glow_r)
            g.setColorAt(0.7, QColor(0, 180, 255, 0))
            g.setColorAt(0.88, QColor(0, 180, 255, glow_alpha))
            g.setColorAt(1.0, QColor(0, 100, 200, 0))
            p.setBrush(QBrush(g))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QRectF(cx - glow_r, cy - glow_r, glow_r * 2, glow_r * 2))

        # ── 2. Main sphere fill (3D shading) ──────────────────────────────
        sphere_grad = QRadialGradient(cx - r * 0.25, cy - r * 0.25, r * 1.1)
        if self._state == self.STATE_SPEAKING:
            sphere_grad.setColorAt(0.0, QColor(0,  60, 100, 255))
            sphere_grad.setColorAt(0.4, QColor(0,  30,  60, 255))
            sphere_grad.setColorAt(1.0, QColor(0,   8,  20, 255))
        elif self._state == self.STATE_LISTENING:
            sphere_grad.setColorAt(0.0, QColor(0,  50, 90, 255))
            sphere_grad.setColorAt(0.4, QColor(0,  25, 55, 255))
            sphere_grad.setColorAt(1.0, QColor(0,   5, 18, 255))
        else:
            sphere_grad.setColorAt(0.0, QColor(0,  35, 65, 255))
            sphere_grad.setColorAt(0.4, QColor(0,  15, 35, 255))
            sphere_grad.setColorAt(1.0, QColor(2,   5, 15, 255))

        p.setBrush(QBrush(sphere_grad))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))

        # ── 3. Latitude wave lines (move when speaking) ───────────────────
        n_lat = 7
        wave_amp = (0.06 + 0.18 * pulse) * r if self._state == self.STATE_SPEAKING \
                   else (0.02 + 0.04 * pulse) * r
        for i in range(n_lat):
            t = (i + 1) / (n_lat + 1)           # 0..1
            lat_y = cy - r + t * r * 2           # actual y on sphere
            # Clamp to sphere boundary
            dy   = lat_y - cy
            half = math.sqrt(max(0, r * r - dy * dy))
            if half < 2:
                continue
            # Build wavy path
            path = QPainterPath()
            steps = 80
            for s in range(steps + 1):
                fx = (s / steps) * 2 * half - half
                angle_along = (s / steps) * 2 * math.pi
                wave_y = lat_y + wave_amp * math.sin(
                    angle_along * 3 + self._wave_phase + i * 0.6
                ) * (1 - abs(dy) / r)
                if s == 0:
                    path.moveTo(cx + fx, wave_y)
                else:
                    path.lineTo(cx + fx, wave_y)

            # Clip to sphere
            clip = QPainterPath()
            clip.addEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))
            p.setClipPath(clip)

            alpha = int(40 + 60 * (1 - abs(t - 0.5) * 2) + 40 * pulse)
            width = 0.8 if self._state != self.STATE_SPEAKING else 1.2
            lat_pen = QPen(QColor(0, 200, 255, alpha), width)
            p.setPen(lat_pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)

        p.setClipping(False)

        # ── 4. Outer border ring ──────────────────────────────────────────
        border_alpha = int(140 + 80 * pulse)
        border_w = 1.5 + pulse
        p.setPen(QPen(QColor(0, 200, 255, border_alpha), border_w))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))

        # ── 5. Rotating dashed outer ring ─────────────────────────────────
        p.save()
        p.translate(cx, cy)
        p.rotate(self._ring_angle)
        dash_r = r + 2
        seg_pen = QPen(QColor(0, 180, 255, 90), 1.2)
        seg_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(seg_pen)
        for i in range(12):
            ang = i * 30
            p.drawArc(QRectF(-dash_r, -dash_r, dash_r * 2, dash_r * 2),
                      int(ang * 16), int(18 * 16))
        p.restore()

        # ── 6. Counter-rotating mid ring ──────────────────────────────────
        p.save()
        p.translate(cx, cy)
        p.rotate(self._ring2_ang)
        mid_r = r * 0.72
        mid_pen = QPen(QColor(0, 220, 255, int(60 + 50 * pulse)), 1.0)
        p.setPen(mid_pen)
        for i in range(6):
            ang = i * 60
            p.drawArc(QRectF(-mid_r, -mid_r, mid_r * 2, mid_r * 2),
                      int(ang * 16), int(35 * 16))
        p.restore()

        # ── 7. Waveform bars around equator ───────────────────────────────
        n_bars = len(self._bars)
        bar_max = r * 0.28
        for i, bar in enumerate(self._bars):
            angle_rad = (i / n_bars) * 2 * math.pi - math.pi / 2
            bar_h = bar * bar_max
            x0 = cx + r * math.cos(angle_rad)
            y0 = cy + r * math.sin(angle_rad)
            x1 = cx + (r + bar_h) * math.cos(angle_rad)
            y1 = cy + (r + bar_h) * math.sin(angle_rad)
            alpha = int(80 + 160 * bar)
            if self._state == self.STATE_SPEAKING:
                color = QColor(0, 220, 255, alpha)
            elif self._state == self.STATE_LISTENING:
                color = QColor(0, 180, 255, alpha)
            else:
                color = QColor(0, 150, 200, int(alpha * 0.5))
            p.setPen(QPen(color, 1.5))
            p.drawLine(QRectF(x0, y0, 0, 0).topLeft(),
                       QRectF(x1, y1, 0, 0).topLeft())

        # ── 8. Core glow ──────────────────────────────────────────────────
        core_r = r * (0.22 + 0.10 * pulse)
        core_grad = QRadialGradient(cx, cy, core_r)
        if self._state == self.STATE_SPEAKING:
            core_grad.setColorAt(0.0, QColor(180, 240, 255, 240))
            core_grad.setColorAt(0.4, QColor(0,   200, 255, 160))
            core_grad.setColorAt(1.0, QColor(0,   100, 200,   0))
        elif self._state == self.STATE_LISTENING:
            core_grad.setColorAt(0.0, QColor(150, 220, 255, 200))
            core_grad.setColorAt(0.4, QColor(0,   170, 255, 130))
            core_grad.setColorAt(1.0, QColor(0,    80, 180,   0))
        else:
            core_grad.setColorAt(0.0, QColor(120, 200, 255, 180))
            core_grad.setColorAt(0.4, QColor(0,   140, 220,  90))
            core_grad.setColorAt(1.0, QColor(0,    60, 160,   0))
        p.setBrush(QBrush(core_grad))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QRectF(cx - core_r, cy - core_r, core_r * 2, core_r * 2))

        # ── 9. J.A.R.V.I.S text ──────────────────────────────────────────
        text_alpha = int(160 + 80 * pulse)
        p.setPen(QColor(0, 220, 255, text_alpha))
        font = QFont("Courier New", max(8, int(self._sz * 0.065)), QFont.Weight.Bold)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 3)
        p.setFont(font)
        p.drawText(QRectF(cx - r, cy - 14, r * 2, 28),
                   Qt.AlignmentFlag.AlignCenter, "J.A.R.V.I.S")

        # ── 10. Status text below name ────────────────────────────────────
        state_text = {"idle": "ONLINE", "listening": "LISTENING",
                      "speaking": "SPEAKING"}.get(self._state, "")
        small_font = QFont("Courier New", max(6, int(self._sz * 0.042)))
        small_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2)
        p.setFont(small_font)
        p.setPen(QColor(0, 180, 255, int(100 + 80 * pulse)))
        p.drawText(QRectF(cx - r, cy + 6, r * 2, 20),
                   Qt.AlignmentFlag.AlignCenter, state_text)

        # ── 11. Specular highlight (top-left shine) ───────────────────────
        shine = QRadialGradient(cx - r * 0.35, cy - r * 0.35, r * 0.45)
        shine.setColorAt(0.0, QColor(255, 255, 255, int(35 + 20 * pulse)))
        shine.setColorAt(1.0, QColor(255, 255, 255, 0))
        clip2 = QPainterPath()
        clip2.addEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))
        p.setClipPath(clip2)
        p.setBrush(QBrush(shine))
        p.setPen(Qt.PenStyle.NoPen)
        shine_sz = r * 0.9
        p.drawEllipse(QRectF(cx - r * 0.35 - shine_sz / 2,
                              cy - r * 0.35 - shine_sz / 2,
                              shine_sz, shine_sz))
        p.setClipping(False)
        p.end()


# ── HUD Background ─────────────────────────────────────────────────────────────

class HUDBackground(QWidget):
    """Full-window animated HUD background: grid, scan line, corner brackets."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._scan_y = 0
        self._scan_alpha = 180
        self._scan_dir = 1
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(20)

    def _tick(self):
        self._scan_y = (self._scan_y + 2) % max(self.height(), 1)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        # Atmospheric gradient wash
        bg = QLinearGradient(0, 0, w, h)
        bg.setColorAt(0.0, QColor(1, 8, 14, 210))
        bg.setColorAt(0.45, QColor(2, 14, 24, 130))
        bg.setColorAt(1.0, QColor(0, 5, 10, 235))
        p.fillRect(self.rect(), bg)

        # Central hologram bloom behind the orb
        bloom = QRadialGradient(w / 2, h * 0.33, min(w, h) * 0.42)
        bloom.setColorAt(0.0, QColor(0, 212, 255, 46))
        bloom.setColorAt(0.55, QColor(0, 160, 255, 18))
        bloom.setColorAt(1.0, QColor(0, 120, 220, 0))
        p.fillRect(self.rect(), bloom)

        # Scanline texture
        scan_pen = QPen(QColor(120, 240, 255, 10))
        p.setPen(scan_pen)
        for y in range(0, h, 5):
            p.drawLine(0, y, w, y)

        # Dot grid
        grid_pen = QPen(QColor(0, 180, 220, 18))
        p.setPen(grid_pen)
        step = 28
        for x in range(0, w, step):
            for y in range(0, h, step):
                p.drawPoint(x, y)

        # Holographic target rings
        p.setPen(QPen(QColor(0, 212, 255, 34), 1))
        center_x = w / 2
        center_y = h * 0.33
        for factor in (0.18, 0.25, 0.33):
            rr = min(w, h) * factor
            p.drawEllipse(QRectF(center_x - rr, center_y - rr, rr * 2, rr * 2))

        # Light beam accents
        beam = QLinearGradient(w * 0.15, 0, w * 0.75, h)
        beam.setColorAt(0.0, QColor(0, 212, 255, 0))
        beam.setColorAt(0.45, QColor(0, 212, 255, 16))
        beam.setColorAt(0.55, QColor(200, 255, 255, 20))
        beam.setColorAt(1.0, QColor(0, 212, 255, 0))
        p.fillRect(self.rect(), beam)

        # Corner bracket — top left
        self._bracket(p, 0, 0, 1, 1, w, h)
        # Corner bracket — top right
        self._bracket(p, w, 0, -1, 1, w, h)
        # Corner bracket — bottom left
        self._bracket(p, 0, h, 1, -1, w, h)
        # Corner bracket — bottom right
        self._bracket(p, w, h, -1, -1, w, h)

        # Scan line with gradient fade
        scan_grad = QLinearGradient(0, self._scan_y - 40, 0, self._scan_y + 40)
        scan_grad.setColorAt(0,   QColor(0, 212, 255, 0))
        scan_grad.setColorAt(0.4, QColor(0, 212, 255, 30))
        scan_grad.setColorAt(0.5, QColor(0, 212, 255, 55))
        scan_grad.setColorAt(0.6, QColor(0, 212, 255, 30))
        scan_grad.setColorAt(1,   QColor(0, 212, 255, 0))
        p.fillRect(0, self._scan_y - 40, w, 80, scan_grad)

        p.end()

    def _bracket(self, p, x, y, dx, dy, w, h):
        arm = min(w, h) * 0.08
        arm = max(20, min(arm, 40))
        pen = QPen(QColor(C_CYAN), 1.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        # horizontal
        p.drawLine(int(x), int(y), int(x + dx * arm), int(y))
        # vertical
        p.drawLine(int(x), int(y), int(x), int(y + dy * arm))


# ── Status Pulse ──────────────────────────────────────────────────────────────

class PulseDot(QWidget):
    """A small pulsing dot indicating online/listening state."""

    def __init__(self, color=C_GREEN, parent=None):
        super().__init__(parent)
        self.setFixedSize(10, 10)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._color = color
        self._alpha = 255
        self._dir = -4
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(25)

    def set_color(self, color):
        self._color = color

    def _tick(self):
        self._alpha += self._dir
        if self._alpha <= 80:
            self._dir = 4
        elif self._alpha >= 255:
            self._dir = -4
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QColor(self._color)
        c.setAlpha(self._alpha)
        p.setBrush(QBrush(c))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(1, 1, 8, 8)
        p.end()


class SignalBars(QWidget):
    """Ambient hologram spectrum bars used around the orb."""

    def __init__(self, bars=18, parent=None):
        super().__init__(parent)
        self._bars = [0.0] * bars
        self._targets = [0.0] * bars
        self._intensity = 0.35
        self.setFixedSize(220, 34)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(55)

    def set_intensity(self, intensity: float):
        self._intensity = max(0.1, min(1.0, intensity))

    def _tick(self):
        for i in range(len(self._targets)):
            if random.random() < 0.35:
                self._targets[i] = random.uniform(0.12, self._intensity)
            self._bars[i] += (self._targets[i] - self._bars[i]) * 0.28
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        h = self.height()
        count = len(self._bars)
        gap = 4
        bar_w = max(3, int((w - (count - 1) * gap) / count))
        base_y = h - 3

        p.setPen(Qt.PenStyle.NoPen)
        for idx, level in enumerate(self._bars):
            bar_h = 5 + int(level * (h - 9))
            x = idx * (bar_w + gap)
            grad = QLinearGradient(x, base_y - bar_h, x, base_y)
            grad.setColorAt(0.0, QColor(215, 250, 255, 220))
            grad.setColorAt(0.3, QColor(0, 212, 255, 190))
            grad.setColorAt(1.0, QColor(0, 100, 170, 55))
            p.setBrush(QBrush(grad))
            p.drawRoundedRect(QRectF(x, base_y - bar_h, bar_w, bar_h), 1.5, 1.5)
        p.end()


class TelemetryPanel(QFrame):
    """Small holographic side panel with live system metrics."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self._value_labels = {}
        self._dot_widgets = {}
        self.setObjectName("TelemetryPanel")
        self.setMinimumWidth(168)
        self.setMaximumWidth(196)
        self.setStyleSheet(_glass_panel_css(fill="rgba(3, 18, 28, 205)", radius=12))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        title_lbl = QLabel(title)
        title_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        title_lbl.setStyleSheet(f"color: {C_CYAN}; background: transparent; letter-spacing: 2px;")
        layout.addWidget(title_lbl)

        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(
            f"background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 transparent, stop:0.25 {C_CYAN}, stop:0.75 {C_CYAN}, stop:1 transparent);"
        )
        layout.addWidget(divider)

        self._rows = QVBoxLayout()
        self._rows.setSpacing(8)
        layout.addLayout(self._rows)
        layout.addStretch()

    def add_metric(self, key: str, label: str, value: str = "--", color: str = C_CYAN):
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)

        dot = PulseDot(color)
        dot.setFixedSize(8, 8)
        row_layout.addWidget(dot, alignment=Qt.AlignmentFlag.AlignTop)
        self._dot_widgets[key] = dot

        text_block = QVBoxLayout()
        text_block.setContentsMargins(0, 0, 0, 0)
        text_block.setSpacing(2)

        label_lbl = QLabel(label)
        label_lbl.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        label_lbl.setStyleSheet(f"color: {C_TEXT_DIM}; background: transparent; letter-spacing: 1px;")
        text_block.addWidget(label_lbl)

        value_lbl = QLabel(value)
        value_lbl.setWordWrap(True)
        value_lbl.setFont(QFont("Courier New", 8))
        value_lbl.setStyleSheet(f"color: {C_WHITE_DIM}; background: transparent;")
        text_block.addWidget(value_lbl)

        self._value_labels[key] = value_lbl
        row_layout.addLayout(text_block, stretch=1)
        self._rows.addWidget(row)

    def set_metric(self, key: str, value: str, color: str | None = None):
        label = self._value_labels.get(key)
        if label is not None:
            label.setText(value)
            label.setToolTip(value)
            if color:
                label.setStyleSheet(f"color: {color}; background: transparent;")
            else:
                label.setStyleSheet(f"color: {C_WHITE_DIM}; background: transparent;")

        dot = self._dot_widgets.get(key)
        if dot is not None and color:
            dot.set_color(color)


# ── Worker threads ─────────────────────────────────────────────────────────────

class VoiceWorker(QThread):
    message = pyqtSignal(str, str, str)
    interaction = pyqtSignal(dict)
    status  = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._active = True

    def stop(self):
        self._active = False

    def run(self):
        while self._active:
            self.status.emit("AWAITING WAKE WORD")
            wait_for_wake_word()
            if not self._active:
                break
            self.status.emit("VOICE ACTIVE")
            self._conversation()

    def _conversation(self):
        speak("Yes?")
        self.message.emit("Yes?", "jarvis", "")
        misses = 0
        exchanges = []

        while self._active:
            self.status.emit("LISTENING")
            user_input = listen()

            if not user_input:
                misses += 1
                if misses >= 2:
                    if exchanges:
                        _summarize(exchanges)
                    self.status.emit("AWAITING WAKE WORD")
                    return
                speak("Still here.")
                self.message.emit("Still here.", "jarvis", "")
                continue

            misses = 0
            lower = user_input.lower().strip()
            self.message.emit(user_input, "user", "")
            exchanges.append(f"User: {user_input}")

            if lower in QUIT_PHRASES:
                if exchanges:
                    _summarize(exchanges)
                speak("Goodbye.")
                self.message.emit("Goodbye.", "jarvis", "")
                self._active = False
                return

            if lower in END_CONVERSATION:
                if exchanges:
                    _summarize(exchanges)
                speak("Alright, I'll be here.")
                self.message.emit("Alright, I'll be here.", "jarvis", "")
                return

            self.status.emit("PROCESSING")
            try:
                stream, model = route_stream(user_input)
                response = speak_stream(stream)
                context_stats = ctx.record_request_stats(model, source="voice_ui")
                entry = evals.log_interaction(user_input, response, model, source="voice_ui", context=context_stats)
                evals.maybe_log_automatic_failure(entry)
                self.interaction.emit(entry)
                self.message.emit(response, "jarvis", model)
                exchanges.append(f"Jarvis: {response}")

                if model == "Self-Improve" and "analyzing" in response.lower():
                    target = re.sub(
                        r"(improve|upgrade|make yourself better at|fix|update|rewrite|enhance)\s*(your|yourself)?\s*",
                        "", user_input.lower()
                    ).strip()
                    def _do_improve(t=target):
                        try:
                            result = si.self_improve(instruction=t if t else None)
                            if "error" in result:
                                msg = result["error"]
                            else:
                                msg = (f"Done. Improved {result['file']} with {result['lines_changed']} "
                                       f"lines changed. Backup saved. Say 'restart yourself' to apply.")
                            speak(msg)
                            self.message.emit(msg, "jarvis", "Self-Improve")
                        except Exception as ex:
                            speak(f"Improvement failed: {ex}")
                    threading.Thread(target=_do_improve, daemon=True).start()

            except Exception as e:
                err = "Sorry, something went wrong."
                speak(err)
                self.message.emit(err, "jarvis", "")


class TextWorker(QThread):
    message = pyqtSignal(str, str, str)
    interaction = pyqtSignal(dict)
    status  = pyqtSignal(str)

    def __init__(self, user_input: str, parent=None):
        super().__init__(parent)
        self.user_input = user_input

    def run(self):
        self.status.emit("PROCESSING")
        try:
            stream, model = route_stream(self.user_input)
            chunks = []
            for chunk in stream:
                chunks.append(chunk)
            response = "".join(chunks)
            context_stats = ctx.record_request_stats(model, source="text_ui")
            entry = evals.log_interaction(self.user_input, response, model, source="text_ui", context=context_stats)
            evals.maybe_log_automatic_failure(entry)
            self.interaction.emit(entry)
            self.message.emit(response, "jarvis", model)
            exchanges = [f"User: {self.user_input}", f"Jarvis: {response}"]
            threading.Thread(
                target=learner.extract_and_learn,
                args=(exchanges,),
                daemon=True
            ).start()
            mem.track_topic(self.user_input)
        except Exception as e:
            self.message.emit(f"Error: {e}", "jarvis", "")
        self.status.emit("ONLINE")


def _summarize(exchanges):
    try:
        summary = ctx.summarize_transcript(exchanges[-10:])
        mem.save_conversation(summary)
        threading.Thread(
            target=learner.extract_and_learn,
            args=(exchanges,),
            daemon=True
        ).start()
    except Exception:
        pass


# ── Message Bubble ─────────────────────────────────────────────────────────────

class MessageBubble(QFrame):
    """HUD-style chat bubble with bracket corners and glow."""

    def __init__(self, text: str, sender: str, model: str = "", parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        is_user = sender == "user"
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(3)

        # Sender label
        label_row = QHBoxLayout()
        label_row.setContentsMargins(0, 0, 0, 0)

        dot = PulseDot(C_ORANGE if is_user else C_CYAN)
        dot.setFixedSize(8, 8)
        label_row.addWidget(dot)

        sender_lbl = QLabel("YOU" if is_user else "J.A.R.V.I.S")
        sender_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        sender_lbl.setStyleSheet(
            f"color: {'#FF6B00' if is_user else C_CYAN}; background: transparent; letter-spacing: 2px;"
        )
        label_row.addWidget(sender_lbl)

        if model and not is_user:
            model_lbl = QLabel(f"[ {model.upper()} ]")
            model_lbl.setFont(QFont("Courier New", 7))
            model_lbl.setStyleSheet(f"color: {C_TEXT_DIM}; background: transparent;")
            label_row.addWidget(model_lbl)

        label_row.addStretch()
        layout.addLayout(label_row)

        # Message text
        msg = QLabel(text)
        msg.setWordWrap(True)
        msg.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        msg.setFont(QFont("Courier New", 12))

        if is_user:
            msg.setStyleSheet(f"""
                color: #FFD9B8;
                background: rgba(34, 15, 2, 220);
                border: 1px solid #FF6B00;
                border-radius: 10px;
                padding: 10px 14px;
            """)
            layout.setAlignment(Qt.AlignmentFlag.AlignRight)
            self.setStyleSheet("background: transparent;")
        else:
            msg.setStyleSheet(f"""
                color: {C_TEXT};
                background: rgba(3, 18, 28, 220);
                border: 1px solid {C_BORDER};
                border-radius: 10px;
                padding: 10px 14px;
            """)
            layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
            self.setStyleSheet("background: transparent;")
            _glow(msg, C_CYAN, 8)

        layout.addWidget(msg)


# ── Main Window ────────────────────────────────────────────────────────────────

class JarvisWindow(QMainWindow):
    _live_transcript_signal = pyqtSignal(str)
    _live_suggestion_signal = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("J.A.R.V.I.S")
        self.setMinimumSize(400, 500)
        self.resize(500, 900)
        self._session_started = datetime.now()
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self._workers = []
        self._last_jarvis_interaction = None
        self._auto_listen_suppressed_meeting = None
        self._auto_listen_engaged_meeting = None
        self._meeting_toolbar_mode = False
        self._meeting_toolbar_auto = False
        self._normal_geometry = None
        self._last_live_listener_started_at = 0.0
        self._last_live_transcript_at = 0.0
        self._last_live_suggestion_at = 0.0
        self._build_ui()
        self._live_updates = LiveUpdateBridge(self)
        self._bind_live_update_signals()
        self._start_voice()

    def _bind_live_update_signals(self):
        if getattr(self, "_live_signals_bound", False):
            return
        if not hasattr(self, "_live_updates"):
            self._live_updates = LiveUpdateBridge(self)
        self._live_updates.transcript.connect(self._apply_live_transcript_update)
        self._live_updates.suggestion.connect(self._apply_live_suggestion_update)
        self._live_signals_bound = True

    def _on_transcript(self, text: str):
        self._live_updates.transcript.emit(text)

    def _on_suggestion(self, suggestion: str):
        self._live_updates.suggestion.emit(suggestion)

    # ── Build UI ───────────────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        central.setAutoFillBackground(True)
        p = central.palette()
        p.setColor(QPalette.ColorRole.Window, QColor(C_BG))
        central.setPalette(p)
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Animated HUD background (lives behind everything)
        self._hud_bg = HUDBackground(central)
        self._hud_bg.setGeometry(0, 0, self.width(), self.height())
        self._hud_bg.lower()

        # ── Header ──────────────────────────────────────────────────────────
        header = QWidget()
        self._header = header
        header.setFixedHeight(70)
        header.setAutoFillBackground(True)
        hp = header.palette()
        hp.setColor(QPalette.ColorRole.Window, QColor(C_BG2))
        header.setPalette(hp)
        header.setStyleSheet(_glass_panel_css(fill="rgba(3, 13, 20, 235)", radius=0) + f"border-bottom: 1px solid {C_BORDER};")

        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(14, 8, 14, 8)
        h_layout.setSpacing(12)

        # Arc reactor
        self._reactor = ArcReactor(size=48)
        h_layout.addWidget(self._reactor)

        # Title block
        title_block = QVBoxLayout()
        title_block.setSpacing(1)

        title = QLabel("J.A.R.V.I.S")
        title.setFont(QFont("Courier New", 16, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C_CYAN}; background: transparent; letter-spacing: 4px;")
        _glow(title, C_CYAN, 14)

        subtitle = QLabel("Just A Rather Very Intelligent System")
        self._subtitle = subtitle
        self._base_subtitle_text = subtitle.text()
        subtitle.setFont(QFont("Courier New", 7))
        subtitle.setStyleSheet(f"color: {C_TEXT_DIM}; background: transparent; letter-spacing: 1px;")

        title_block.addWidget(title)
        title_block.addWidget(subtitle)
        h_layout.addLayout(title_block)
        h_layout.addStretch()

        # Status block
        status_block = QVBoxLayout()
        status_block.setSpacing(4)
        status_block.setAlignment(Qt.AlignmentFlag.AlignRight)

        status_row = QHBoxLayout()
        status_row.setSpacing(5)
        self._status_dot = PulseDot(C_GREEN)
        self._status_label = QLabel("ONLINE")
        self._status_label.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        self._status_label.setStyleSheet(f"color: {C_GREEN}; background: transparent; letter-spacing: 2px;")
        status_row.addWidget(self._status_dot)
        status_row.addWidget(self._status_label)
        status_block.addLayout(status_row)

        mode_lbl = QLabel(f"MODE: {model_router.get_mode().upper()}")
        mode_lbl.setFont(QFont("Courier New", 7))
        mode_lbl.setStyleSheet(f"color: {C_TEXT_DIM}; background: transparent;")
        self._mode_lbl = mode_lbl
        status_block.addWidget(mode_lbl, alignment=Qt.AlignmentFlag.AlignRight)

        h_layout.addLayout(status_block)

        # Overlay launch button
        self.overlay_btn = QPushButton("⬡ ASSIST")
        self.overlay_btn.setFixedHeight(28)
        self.overlay_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self.overlay_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {C_CYAN};
                border: 1px solid {C_CYAN};
                border-radius: 3px;
                padding: 0 10px;
                letter-spacing: 1px;
            }}
            QPushButton:hover {{ background: {C_BLUE}; }}
        """)
        self.overlay_btn.setToolTip("Toggle Meeting Assist overlay (Cmd+Opt+O)")
        self.overlay_btn.clicked.connect(_overlay_mod.toggle)
        h_layout.addWidget(self.overlay_btn)

        self.devices_btn = QPushButton("◎ DEVICES")
        self.devices_btn.setFixedHeight(28)
        self.devices_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self.devices_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {C_TEXT_DIM};
                border: 1px solid {C_BORDER};
                border-radius: 3px;
                padding: 0 10px;
                letter-spacing: 1px;
            }}
            QPushButton:hover {{
                color: {C_CYAN};
                border-color: {C_CYAN};
                background: {C_BLUE};
            }}
        """)
        self.devices_btn.setToolTip("Show nearby devices and bridge actions")
        self.devices_btn.clicked.connect(self._toggle_device_panel)
        h_layout.addWidget(self.devices_btn)

        self.compact_btn = QPushButton("▁")
        self.compact_btn.setFixedSize(28, 28)
        self.compact_btn.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        self.compact_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {C_TEXT_DIM};
                border: 1px solid {C_BORDER};
                border-radius: 3px;
            }}
            QPushButton:hover {{
                color: {C_CYAN};
                border-color: {C_CYAN};
                background: {C_BLUE};
            }}
        """)
        self.compact_btn.setToolTip("Toggle compact meeting bar")
        self.compact_btn.clicked.connect(self._toggle_meeting_toolbar_mode)
        h_layout.addWidget(self.compact_btn)

        root.addWidget(header)

        # ── Divider ──────────────────────────────────────────────────────────
        div = QLabel()
        self._div_top = div
        div.setFixedHeight(1)
        div.setStyleSheet(f"background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 transparent, stop:0.3 {C_CYAN}, stop:0.7 {C_CYAN}, stop:1 transparent);")
        root.addWidget(div)

        # ── Orb panel ────────────────────────────────────────────────────────
        orb_panel = QWidget()
        self._orb_panel = orb_panel
        orb_panel.setFixedHeight(340)
        orb_panel.setStyleSheet("background: transparent;")
        orb_layout = QHBoxLayout(orb_panel)
        orb_layout.setContentsMargins(14, 10, 14, 10)
        orb_layout.setSpacing(16)

        self._left_telemetry = TelemetryPanel("SYSTEM MATRIX")
        self._left_telemetry.add_metric("mode", "OPERATING MODE")
        self._left_telemetry.add_metric("status", "CURRENT STATE")
        self._left_telemetry.add_metric("memory", "MEMORY NODES")
        self._left_telemetry.add_metric("recent", "RECENT FAILURES")
        orb_layout.addWidget(self._left_telemetry, alignment=Qt.AlignmentFlag.AlignVCenter)

        center_stack = QVBoxLayout()
        center_stack.setContentsMargins(0, 0, 0, 0)
        center_stack.setSpacing(6)
        center_stack.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._orb = JarvisOrb(size=260)
        center_stack.addWidget(self._orb, alignment=Qt.AlignmentFlag.AlignCenter)

        self._signal_bars = SignalBars()
        center_stack.addWidget(self._signal_bars, alignment=Qt.AlignmentFlag.AlignCenter)
        orb_layout.addLayout(center_stack, stretch=1)

        self._right_telemetry = TelemetryPanel("TACTICAL FEED")
        self._right_telemetry.add_metric("uptime", "SESSION UPTIME")
        self._right_telemetry.add_metric("messages", "MESSAGE COUNT")
        self._right_telemetry.add_metric("workers", "ACTIVE THREADS")
        self._right_telemetry.add_metric("clock", "LOCAL CLOCK")
        orb_layout.addWidget(self._right_telemetry, alignment=Qt.AlignmentFlag.AlignVCenter)
        root.addWidget(orb_panel)

        # Divider below orb
        div1b = QLabel()
        self._div_after_orb = div1b
        div1b.setFixedHeight(1)
        div1b.setStyleSheet(div.styleSheet())
        root.addWidget(div1b)

        # ── Chat area ────────────────────────────────────────────────────────
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setStyleSheet(f"""
            QScrollArea {{ background: transparent; border: none; }}
            QScrollBar:vertical {{
                background: {C_BG2}; width: 4px; border-radius: 2px;
            }}
            QScrollBar::handle:vertical {{
                background: {C_BORDER}; border-radius: 2px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
        """)

        self.chat_widget = QWidget()
        self.chat_widget.setAutoFillBackground(False)
        self.chat_widget.setStyleSheet("background: transparent;")
        self.chat_layout = QVBoxLayout(self.chat_widget)
        self.chat_layout.setContentsMargins(14, 14, 14, 14)
        self.chat_layout.setSpacing(10)
        self.chat_layout.addStretch()

        self.scroll.setWidget(self.chat_widget)
        self.scroll.setStyleSheet(f"background: transparent; border: none;")
        root.addWidget(self.scroll, stretch=1)

        # ── Divider ──────────────────────────────────────────────────────────
        div2 = QLabel()
        self._div_before_input = div2
        div2.setFixedHeight(1)
        div2.setStyleSheet(div.styleSheet())
        root.addWidget(div2)

        # ── Input area ───────────────────────────────────────────────────────
        input_bar = QWidget()
        self._input_bar = input_bar
        input_bar.setFixedHeight(68)
        input_bar.setAutoFillBackground(True)
        ip = input_bar.palette()
        ip.setColor(QPalette.ColorRole.Window, QColor(C_BG2))
        input_bar.setPalette(ip)
        input_bar.setStyleSheet(_glass_panel_css(fill="rgba(3, 13, 20, 235)", radius=0))

        i_layout = QHBoxLayout(input_bar)
        i_layout.setContentsMargins(12, 10, 12, 10)
        i_layout.setSpacing(8)

        self.attach_btn = self._hud_btn("📎")
        self.attach_btn.setToolTip("Attach a file")
        self.attach_btn.clicked.connect(self._attach_file)

        self.listen_btn = self._hud_btn("🎧")
        self.listen_btn.setToolTip("Smart Listen — tap into call audio (Cmd+Shift+M)")
        self.listen_btn.clicked.connect(self._toggle_smart_listen)

        self.scan_btn = self._hud_btn("⌁")
        self.scan_btn.setToolTip("Analyze the current screen")
        self.scan_btn.clicked.connect(self._hotkey_screen)

        self.cam_btn = self._hud_btn("◉")
        self.cam_btn.setToolTip("Analyze the current camera frame")
        self.cam_btn.clicked.connect(self._hotkey_webcam)

        self.flag_btn = self._hud_btn("⚑")
        self.flag_btn.setToolTip("Flag the last Jarvis answer for evals")
        self.flag_btn.clicked.connect(self._flag_last_answer)

        self.input_field = EnterLineEdit()
        self.input_field.setPlaceholderText("ENTER COMMAND...")
        self.input_field.setFont(QFont("Courier New", 12))
        self.input_field.setStyleSheet(f"""
            QLineEdit {{
                background: {C_BG};
                color: {C_TEXT};
                border: 1px solid {C_BORDER};
                border-radius: 2px;
                padding: 7px 12px;
                letter-spacing: 1px;
            }}
            QLineEdit:focus {{
                border: 1px solid {C_CYAN};
            }}
        """)
        self.input_field.returnPressed.connect(self._send_text)

        self.send_btn = QPushButton("▶")
        self.send_btn.setFixedSize(40, 40)
        self.send_btn.setFont(QFont("Courier New", 13, QFont.Weight.Bold))
        self.send_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {C_CYAN};
                border: 1px solid {C_CYAN};
                border-radius: 2px;
            }}
            QPushButton:hover {{
                background: {C_BLUE};
                color: white;
            }}
            QPushButton:pressed {{
                background: {C_CYAN};
                color: {C_BG};
            }}
        """)
        _glow(self.send_btn, C_CYAN, 10)
        self.send_btn.clicked.connect(self._send_text)

        i_layout.addWidget(self.attach_btn)
        i_layout.addWidget(self.listen_btn)
        i_layout.addWidget(self.scan_btn)
        i_layout.addWidget(self.cam_btn)
        i_layout.addWidget(self.flag_btn)
        i_layout.addWidget(self.input_field, stretch=1)
        i_layout.addWidget(self.send_btn)
        root.addWidget(input_bar)

        # ── Smart Listen panel ───────────────────────────────────────────────
        self.suggest_panel = QWidget()
        self.suggest_panel.setAutoFillBackground(True)
        sp = self.suggest_panel.palette()
        sp.setColor(QPalette.ColorRole.Window, QColor("#010D18"))
        self.suggest_panel.setPalette(sp)
        self.suggest_panel.setStyleSheet(_glass_panel_css(fill="rgba(1, 13, 24, 230)", radius=0) + f"border-top: 1px solid {C_BORDER};")
        self.suggest_panel.hide()

        sl = QVBoxLayout(self.suggest_panel)
        sl.setContentsMargins(14, 8, 14, 8)
        sl.setSpacing(4)

        sh = QHBoxLayout()
        lbl = QLabel("🎧  SMART LISTEN  —  ACTIVE")
        lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        lbl.setStyleSheet(f"color: {C_CYAN}; background: transparent; letter-spacing: 2px;")
        self._listen_status = PulseDot(C_GREEN)
        sh.addWidget(lbl)
        sh.addStretch()
        sh.addWidget(self._listen_status)
        sl.addLayout(sh)

        self.suggest_label = QLabel("Listening to call...")
        self.suggest_label.setWordWrap(True)
        self.suggest_label.setFont(QFont("Courier New", 11))
        self.suggest_label.setStyleSheet(f"""
            color: {C_TEXT};
            background: {C_PANEL};
            border: 1px solid {C_BORDER};
            border-radius: 2px;
            padding: 8px 12px;
        """)
        self.suggest_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        sl.addWidget(self.suggest_label)

        self.transcript_label = QLabel("")
        self.transcript_label.setWordWrap(True)
        self.transcript_label.setFont(QFont("Courier New", 8))
        self.transcript_label.setStyleSheet(f"color: {C_TEXT_DIM}; background: transparent;")
        sl.addWidget(self.transcript_label)

        root.addWidget(self.suggest_panel)

        # ── Nearby devices panel ────────────────────────────────────────────
        self.device_panel = QWidget()
        self.device_panel.setAutoFillBackground(True)
        dp = self.device_panel.palette()
        dp.setColor(QPalette.ColorRole.Window, QColor("#010C14"))
        self.device_panel.setPalette(dp)
        self.device_panel.setStyleSheet(
            _glass_panel_css(fill="rgba(1, 12, 20, 228)", radius=0) +
            f"border-top: 1px solid {C_BORDER};"
        )
        self.device_panel.hide()

        dl = QVBoxLayout(self.device_panel)
        dl.setContentsMargins(14, 8, 14, 10)
        dl.setSpacing(6)

        dh = QHBoxLayout()
        dh.setSpacing(6)
        dlbl = QLabel("◎  NEARBY DEVICES  —  BRIDGE")
        dlbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        dlbl.setStyleSheet(f"color: {C_CYAN}; background: transparent; letter-spacing: 2px;")
        dh.addWidget(dlbl)
        dh.addStretch()

        self.device_refresh_btn = self._hud_btn("↻")
        self.device_refresh_btn.setFixedSize(34, 34)
        self.device_refresh_btn.setToolTip("Refresh nearby devices")
        self.device_refresh_btn.clicked.connect(self._refresh_nearby_devices)
        dh.addWidget(self.device_refresh_btn)
        dl.addLayout(dh)

        self.device_summary = QTextEdit()
        self.device_summary.setReadOnly(True)
        self.device_summary.setMinimumHeight(140)
        self.device_summary.setMaximumHeight(220)
        self.device_summary.setFont(QFont("Courier New", 9))
        self.device_summary.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.device_summary.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.device_summary.setStyleSheet(f"""
            QTextEdit {{
                background: {C_PANEL};
                color: {C_TEXT};
                border: 1px solid {C_BORDER};
                border-radius: 2px;
                padding: 8px 10px;
            }}
        """)
        dl.addWidget(self.device_summary)

        self.device_action_row = QHBoxLayout()
        self.device_action_row.setSpacing(8)

        self.device_bt_btn = self._hud_btn("BT")
        self.device_bt_btn.setToolTip("Open Bluetooth settings")
        self.device_bt_btn.clicked.connect(lambda: self._open_device_settings("bluetooth"))
        self.device_action_row.addWidget(self.device_bt_btn)

        self.device_sound_btn = self._hud_btn("AIR")
        self.device_sound_btn.setToolTip("Open Sound / AirPlay settings")
        self.device_sound_btn.clicked.connect(lambda: self._open_device_settings("sound"))
        self.device_action_row.addWidget(self.device_sound_btn)

        self.device_display_btn = self._hud_btn("DSP")
        self.device_display_btn.setToolTip("Open Displays settings")
        self.device_display_btn.clicked.connect(lambda: self._open_device_settings("displays"))
        self.device_action_row.addWidget(self.device_display_btn)

        self.device_bridge_btn = self._hud_btn("URL")
        self.device_bridge_btn.setToolTip("Copy Jarvis bridge URL")
        self.device_bridge_btn.clicked.connect(self._copy_bridge_url)
        self.device_action_row.addWidget(self.device_bridge_btn)

        self.device_page_btn = self._hud_btn("TAB")
        self.device_page_btn.setToolTip("Copy current browser page URL")
        self.device_page_btn.clicked.connect(self._copy_current_page_url)
        self.device_action_row.addWidget(self.device_page_btn)

        self.device_action_row.addStretch()
        dl.addLayout(self.device_action_row)

        root.addWidget(self.device_panel)

        self._telemetry_timer = QTimer(self)
        self._telemetry_timer.timeout.connect(self._refresh_telemetry)
        self._telemetry_timer.start(1200)
        self._device_timer = QTimer(self)
        self._device_timer.timeout.connect(self._refresh_nearby_devices)
        self._device_timer.start(15000)
        self._refresh_telemetry()
        self._refresh_nearby_devices()
        self._update_meeting_toolbar_layout()

    def _hud_btn(self, label: str) -> QPushButton:
        btn = QPushButton(label)
        btn.setFixedSize(40, 40)
        btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: 1px solid {C_BORDER};
                border-radius: 2px;
                font-size: 15px;
            }}
            QPushButton:hover {{
                background: {C_BLUE};
                border-color: {C_CYAN};
            }}
        """)
        return btn

    def _set_device_panel_visible(self, visible: bool):
        if self._meeting_toolbar_mode and visible:
            self._add_message("Nearby Devices stays hidden while meeting toolbar mode is active.", "jarvis", "Hardware")
            return
        self.device_panel.setVisible(visible)
        self.devices_btn.setText("◉ DEVICES" if visible else "◎ DEVICES")
        self.devices_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {C_CYAN if visible else C_TEXT_DIM};
                border: 1px solid {C_CYAN if visible else C_BORDER};
                border-radius: 3px;
                padding: 0 10px;
                letter-spacing: 1px;
            }}
            QPushButton:hover {{
                color: {C_CYAN};
                border-color: {C_CYAN};
                background: {C_BLUE};
            }}
        """)
        if visible:
            self._refresh_nearby_devices()

    def _toggle_device_panel(self):
        self._set_device_panel_visible(not self.device_panel.isVisible())

    def _bridge_snapshot(self) -> dict:
        return _bridge_snapshot_data()

    def _format_nearby_snapshot(self, snapshot: dict) -> str:
        bluetooth = snapshot.get("bluetooth", {})
        network = snapshot.get("network", {}).get("services", {})
        bridge = self._bridge_snapshot()

        connected_bt = [item.get("name", "") for item in bluetooth.get("connected", []) if item.get("name")]
        known_bt = [item.get("name", "") for item in bluetooth.get("known", []) if item.get("name")]
        airplay = [item.get("name", "") for item in network.get("airplay", []) if item.get("name")]
        raop = [item.get("name", "") for item in network.get("raop", []) if item.get("name")]
        companion = [item.get("name", "") for item in network.get("companion", []) if item.get("name")]
        cast = [item.get("name", "") for item in network.get("googlecast", []) if item.get("name")]

        lines = [
            f"Bridge: {'LAN enabled' if bridge.get('enabled') else 'Local only'}",
            f"Primary URL: {bridge.get('primary_url', 'unavailable')}",
        ]
        if bridge.get("ips"):
            lines.append("LAN IPs: " + ", ".join(bridge["ips"]))

        lines.extend([
            "",
            "Bluetooth connected: " + (", ".join(connected_bt[:4]) if connected_bt else "none active"),
            "Bluetooth known: " + (", ".join(known_bt[:6]) if known_bt else "none found"),
            "AirPlay targets: " + (", ".join(airplay[:6]) if airplay else "none found"),
            "Audio targets: " + (", ".join(raop[:6]) if raop else "none found"),
            "Nearby Apple devices: " + (", ".join(companion[:6]) if companion else "none found"),
            "Google Cast: " + (", ".join(cast[:6]) if cast else "none found"),
            "",
            "Actions:",
            "BT = Bluetooth settings   AIR = Sound/AirPlay   DSP = Displays",
            "URL = copy bridge URL     TAB = copy current page URL",
        ])
        return "\n".join(lines)

    def _refresh_nearby_devices(self):
        if not hasattr(self, "device_summary"):
            return
        try:
            snapshot = _device_refresh_snapshot(timeout=1.2)
            text = self._format_nearby_snapshot(snapshot)
        except Exception as e:
            text = f"Nearby device scan failed: {e}"
        self.device_summary.setPlainText(text)
        self.device_summary.moveCursor(QTextCursor.MoveOperation.Start)

    def _open_device_settings(self, target: str):
        msg = _device_open_settings(target)
        self._add_message(msg, "jarvis", "Hardware")

    def _copy_bridge_url(self):
        bridge = self._bridge_snapshot()
        url = bridge.get("primary_url", "")
        if not url:
            self._add_message("No bridge URL is available yet.", "jarvis", "Hardware")
            return
        handled_msg = _device_copy_bridge_url()
        if handled_msg is None:
            terminal.set_clipboard(url)
        mode = "LAN" if bridge.get("enabled") else "local-only"
        self._add_message(handled_msg or f"Copied the {mode} Jarvis URL: {url}", "jarvis", "Hardware")

    def _copy_current_page_url(self):
        page = browser.get_current_page_info()
        if not page.get("ok") or not page.get("url"):
            self._add_message(page.get("error", "I couldn't read the current browser page."), "jarvis", "Browser")
            return
        handled_msg = _device_copy_current_page_url()
        if handled_msg is None:
            terminal.set_clipboard(page["url"])
        title = page.get("title") or "Current page"
        self._add_message(handled_msg or f"Copied page URL for {title}: {page['url']}", "jarvis", "Browser")

    # ── Resize: keep HUD background covering full window ──────────────────────

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_hud_bg"):
            self._hud_bg.setGeometry(0, 0, self.width(), self.height())
        wide = self.width() >= 760
        if hasattr(self, "_left_telemetry"):
            self._left_telemetry.setVisible(wide)
        if hasattr(self, "_right_telemetry"):
            self._right_telemetry.setVisible(wide)

    # ── Voice & startup ────────────────────────────────────────────────────────

    def _start_voice(self):
        set_timer_callback(self._on_timer_done)
        learner.start_background_feed()

        # Start proactive agents
        agents.start(on_alert=self._on_agent_alert)

        threading.Thread(target=self._maybe_brief, daemon=True).start()

        self.voice_worker = VoiceWorker()
        self.voice_worker.message.connect(self._add_message)
        self.voice_worker.interaction.connect(self._register_interaction)
        self.voice_worker.status.connect(self._set_status)
        self.voice_worker.start()

        # Poll voice speaking state → drive orb animation
        from voice import _done_speaking as _voice_speaking_event
        self._voice_event = _voice_speaking_event
        self._orb_poll = QTimer(self)
        self._orb_poll.timeout.connect(self._sync_orb_to_voice)
        self._orb_poll.start(80)

        QTimer.singleShot(500, lambda: stealth.apply_stealth(int(self.winId())))

        # Create overlay (hidden by default)
        self._overlay = _overlay_mod.get_overlay()

        hotkeys.register(
            on_screen=self._hotkey_screen,
            on_webcam=self._hotkey_webcam,
            on_clip=self._hotkey_clipboard,
            on_toggle=self._hotkey_toggle,
            on_listen=self._toggle_smart_listen,
            on_overlay=_overlay_mod.toggle,
        )
        hotkeys.start()

        self._meeting_watchdog_timer = QTimer(self)
        self._meeting_watchdog_timer.timeout.connect(self._meeting_watchdog_tick)
        self._meeting_watchdog_tick()
        self._meeting_watchdog_timer.start(1000)

    # ── Hotkey handlers ────────────────────────────────────────────────────────

    def _hotkey_screen(self):
        self._set_status("SCANNING DISPLAY")
        try:
            import camera
            result = camera.screenshot_and_describe(
                "Analyze this screenshot in detail. The user is sharing this with you privately "
                "during a call. Describe what you see, highlight anything important or actionable."
            )
            speak(result)
            self._add_message(result, "jarvis", "")
        except Exception as e:
            self._add_message(f"Screen capture failed: {e}", "jarvis", "")
        self._set_status("ONLINE")

    def _hotkey_webcam(self):
        self._set_status("CAMERA ACTIVE")
        try:
            import camera
            result = camera.see(
                "Describe what you see in this webcam frame in detail. "
                "The user is sharing this with you privately."
            )
            speak(result)
            self._add_message(result, "jarvis", "")
        except Exception as e:
            self._add_message(f"Camera capture failed: {e}", "jarvis", "")
        self._set_status("ONLINE")

    def _hotkey_clipboard(self):
        self._set_status("READING CLIPBOARD")
        try:
            import terminal
            content = terminal.get_clipboard()
            if not content or content == "Clipboard is empty.":
                speak("Your clipboard is empty.")
                return
            self._add_message(f"📋 Clipboard: {content[:100]}{'...' if len(content) > 100 else ''}", "user", "")
            worker = TextWorker(f"The user just copied this text. Analyze it and respond helpfully:\n\n{content}")
            worker.message.connect(self._add_message)
            worker.status.connect(self._set_status)
            worker.start()
            self._workers.append(worker)
        except Exception as e:
            self._add_message(f"Clipboard read failed: {e}", "jarvis", "")

    def _hotkey_toggle(self):
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()
            self.activateWindow()

    # ── Smart Listen ───────────────────────────────────────────────────────────

    def _toggle_smart_listen(self):
        if _meeting_is_running():
            msg = _meeting_stop()
            self.suggest_panel.hide()
            self._add_message(msg, "jarvis", "")
            meeting = _overlay_mod.detect_meeting_app() or "NONE"
            self._auto_listen_suppressed_meeting = meeting if meeting != "NONE" else None
            self._auto_listen_engaged_meeting = None
            self._last_live_listener_started_at = 0.0
            self._last_live_transcript_at = 0.0
            self._last_live_suggestion_at = 0.0
            self.transcript_label.setText("Smart Listen offline.")
            self._subtitle.setText(self._base_subtitle_text)
            self.listen_btn.setText("🎧")
            self._set_status("ONLINE")
        else:
            msg = _meeting_start(
                on_transcript=self._on_transcript,
                on_suggestion=self._on_suggestion,
            )
            self.suggest_panel.show()
            self._add_message(msg, "jarvis", "")
            meeting = _overlay_mod.detect_meeting_app() or "NONE"
            self._auto_listen_suppressed_meeting = None
            self._auto_listen_engaged_meeting = meeting if meeting != "NONE" else None
            self._last_live_listener_started_at = 0.0
            self._last_live_transcript_at = 0.0
            self._last_live_suggestion_at = 0.0
            self.transcript_label.setText("Live call transcript incoming...")
            self._subtitle.setText("Smart Listen active")
            self.listen_btn.setText("■")
            self._set_status("LISTENING")
        self._update_meeting_toolbar_layout()

    def _apply_live_transcript_update(self, text: str):
        _trace_ui_event(self, "classic-toolbar", "live_transcript", text)
        _force_text_widget_update(self.transcript_label, f"Transcript: {text[:240]}")
        self.suggest_panel.show()
        self._update_meeting_toolbar_layout()

    def _apply_live_suggestion_update(self, suggestion: str):
        _trace_ui_event(self, "classic-toolbar", "live_suggestion", suggestion)
        _force_text_widget_update(self.suggest_label, suggestion)
        self.suggest_panel.show()
        if suggestion:
            self._add_message(suggestion, "jarvis", "Meeting")
        self._update_meeting_toolbar_layout()

    def _show_suggestion(self, suggestion: str):
        self._apply_live_suggestion_update(suggestion)

    # ── Briefing ───────────────────────────────────────────────────────────────

    def _maybe_brief(self):
        learner.reflect()
        self._set_status("ONLINE")

    def _meeting_watchdog_tick(self):
        meeting = _overlay_mod.detect_meeting_app() or "NONE"
        snapshot = _meeting_status_snapshot()
        live = _live_listener_snapshot(snapshot)
        preferred = live.get("preferred", {}) or snapshot.get("preferred_source", {}) or snapshot.get("preferred", {})
        listener_started_at = float(live.get("started_at") or snapshot.get("started_at") or 0.0)
        if listener_started_at > self._last_live_listener_started_at:
            self._last_live_listener_started_at = listener_started_at
            self._last_live_transcript_at = 0.0
            self._last_live_suggestion_at = 0.0
        last_transcript = (live.get("last_transcript") or snapshot.get("last_transcript") or "").strip()
        last_suggestion = (live.get("last_suggestion") or snapshot.get("last_suggestion") or "").strip()
        last_transcript_at = float(live.get("last_transcript_at") or snapshot.get("last_transcript_at") or 0.0)
        last_suggestion_at = float(live.get("last_suggestion_at") or snapshot.get("last_suggestion_at") or 0.0)

        if last_transcript and last_transcript_at > self._last_live_transcript_at:
            self._last_live_transcript_at = last_transcript_at
            self._apply_live_transcript_update(last_transcript)
        if last_suggestion and last_suggestion_at > self._last_live_suggestion_at:
            self._last_live_suggestion_at = last_suggestion_at
            self._apply_live_suggestion_update(last_suggestion)

        if meeting == "NONE":
            if self._meeting_toolbar_mode and self._meeting_toolbar_auto:
                self._set_meeting_toolbar_mode(False, auto=True)
            if not bool(live.get("running", snapshot.get("running", False))):
                self._auto_listen_suppressed_meeting = None
                self._auto_listen_engaged_meeting = None
                self._subtitle.setText(self._base_subtitle_text)
                self.listen_btn.setText("🎧")
                self._set_status("ONLINE")
                self._last_live_listener_started_at = 0.0
                self._last_live_transcript_at = 0.0
                self._last_live_suggestion_at = 0.0
            return

        if not self._meeting_toolbar_mode:
            self._set_meeting_toolbar_mode(True, auto=True)

        if bool(live.get("running", snapshot.get("running", False))):
            self._auto_listen_engaged_meeting = meeting
            self._subtitle.setText("Smart Listen active")
            self.listen_btn.setText("■")
            return

        if self._auto_listen_suppressed_meeting == meeting:
            return

        if preferred.get("kind") == "microphone":
            return

        self._last_live_listener_started_at = 0.0
        self._last_live_transcript_at = 0.0
        self._last_live_suggestion_at = 0.0
        msg = _meeting_start(
            on_transcript=self._on_transcript,
            on_suggestion=self._on_suggestion,
        )
        self.suggest_panel.show()
        self._add_message(msg, "jarvis", "")
        self.transcript_label.setText("Live call transcript incoming...")
        self._subtitle.setText("Smart Listen active")
        self.listen_btn.setText("■")
        self._set_status("LISTENING")
        self._auto_listen_engaged_meeting = meeting
        self._update_meeting_toolbar_layout()

    def _toggle_meeting_toolbar_mode(self):
        self._set_meeting_toolbar_mode(not self._meeting_toolbar_mode, auto=False)

    def _position_meeting_toolbar(self):
        screen = QApplication.screenAt(self.frameGeometry().center()) or QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        target_x = geo.x() + max(16, geo.width() - self.width() - 20)
        target_y = geo.y() + 22
        self.move(target_x, target_y)

    def _update_meeting_toolbar_layout(self):
        if not self._meeting_toolbar_mode:
            return
        panel_height = 112 if self.suggest_panel.isVisible() else 0
        target_height = 146 + panel_height
        self.resize(max(self.width(), 560), target_height)
        self._position_meeting_toolbar()

    def _set_meeting_toolbar_mode(self, enabled: bool, auto: bool):
        if enabled == self._meeting_toolbar_mode:
            if enabled:
                self._meeting_toolbar_auto = auto
                self._update_meeting_toolbar_layout()
            return

        self._meeting_toolbar_mode = enabled
        self._meeting_toolbar_auto = auto

        if enabled:
            self._normal_geometry = self.geometry()
            self.setMinimumSize(560, 146)
            self.device_panel.hide()
            self.devices_btn.setText("◎ DEVICES")
            self.devices_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent;
                    color: {C_TEXT_DIM};
                    border: 1px solid {C_BORDER};
                    border-radius: 3px;
                    padding: 0 10px;
                    letter-spacing: 1px;
                }}
                QPushButton:hover {{
                    color: {C_CYAN};
                    border-color: {C_CYAN};
                    background: {C_BLUE};
                }}
            """)
            self._orb_panel.hide()
            self._div_after_orb.hide()
            self.scroll.hide()
            self._div_before_input.hide()
            self._subtitle.setText("Meeting toolbar mode")
            self.compact_btn.setText("▣")
            self.compact_btn.setToolTip("Restore full Jarvis window")
            self._update_meeting_toolbar_layout()
            self.raise_()
            self.activateWindow()
            return

        self.setMinimumSize(400, 500)
        self._orb_panel.show()
        self._div_after_orb.show()
        self.scroll.show()
        self._div_before_input.show()
        self._subtitle.setText("Just A Rather Very Intelligent System")
        self.compact_btn.setText("▁")
        self.compact_btn.setToolTip("Toggle compact meeting bar")
        if self._normal_geometry is not None:
            self.setGeometry(self._normal_geometry)

    def _sync_orb_to_voice(self):
        """Keep orb state in sync with actual TTS playback."""
        if hasattr(self, "_voice_event"):
            speaking = not self._voice_event.is_set()  # cleared = speaking
            if speaking:
                self._orb.set_state(JarvisOrb.STATE_SPEAKING)

    def _refresh_telemetry(self):
        if not hasattr(self, "_left_telemetry"):
            return

        memory_data = mem.load()
        recent_failures = len(evals.recent_failures(limit=12, hours=24))
        facts = len(memory_data.get("facts", []))
        working = memory_data.get("working_memory", {})
        focus = len(working.get("recent_focus", []))
        message_count = max(0, self.chat_layout.count() - 1) if hasattr(self, "chat_layout") else 0
        active_workers = sum(1 for worker in self._workers if worker.isRunning())
        uptime_seconds = int((datetime.now() - self._session_started).total_seconds())
        uptime = f"{uptime_seconds // 60:02d}m {uptime_seconds % 60:02d}s"
        status = self._status_label.text().replace("AWAITING WAKE WORD", "STANDBY")

        self._left_telemetry.set_metric("mode", model_router.get_mode().upper(), C_CYAN)
        self._left_telemetry.set_metric("status", status, self._status_color_for_text(status))
        self._left_telemetry.set_metric("memory", f"{facts} facts / {focus} focus", C_WHITE_DIM)
        self._left_telemetry.set_metric("recent", f"{recent_failures} in 24h", C_WARNING if recent_failures else C_GREEN)

        self._right_telemetry.set_metric("uptime", uptime, C_WHITE_DIM)
        self._right_telemetry.set_metric("messages", str(message_count), C_CYAN)
        self._right_telemetry.set_metric("workers", str(active_workers), C_WARNING if active_workers else C_GREEN)
        self._right_telemetry.set_metric("clock", datetime.now().strftime("%H:%M:%S"), C_WHITE_DIM)

    def _status_color_for_text(self, text: str) -> str:
        upper = (text or "").upper()
        if "LISTEN" in upper or "ACTIVE" in upper or "VOICE" in upper:
            return C_CYAN
        if "PROCESS" in upper or "SCAN" in upper or "CAMERA" in upper or "READING" in upper:
            return C_WARNING
        return C_GREEN

    def _on_agent_alert(self, title: str, body: str, speak_it: bool):
        """Called from agent threads — must marshal to UI thread via QTimer."""
        def _show():
            self._add_message(f"[{title}]  {body}", "jarvis", "Agent")
            if speak_it:
                threading.Thread(target=speak, args=(body,), daemon=True).start()
        QTimer.singleShot(0, _show)

    def _on_timer_done(self, label: str):
        msg = f"Time's up. Your {label} timer is done."
        speak(msg)
        self._add_message(msg, "jarvis", "")

    # ── Chat ───────────────────────────────────────────────────────────────────

    def _show_toolbar_message(self, text: str, sender: str, model: str):
        surface_visible = self._meeting_toolbar_mode or self.suggest_panel.isVisible()
        event = "manual_prompt" if sender == "user" else "manual_response"
        _trace_ui_event(self, "classic-toolbar", event, text, model=model, toolbar=surface_visible)

        if sender == "user":
            preview = text.strip()
            if len(preview) > 220:
                preview = preview[:217].rstrip() + "..."
            self.suggest_label.setText(f"Working on: {preview}")
            self.transcript_label.setText("Generating response...")
            self.suggest_panel.show()
        else:
            self.suggest_label.setText(text)
            if model:
                self.transcript_label.setText(f"[{model}] Manual response ready.")
            else:
                self.transcript_label.setText("Manual response ready.")

            self.suggest_panel.show()
        if self._meeting_toolbar_mode:
            self._update_meeting_toolbar_layout()

    def _add_message(self, text: str, sender: str, model: str):
        bubble = MessageBubble(text, sender, model)
        count = self.chat_layout.count()
        self.chat_layout.insertWidget(count - 1, bubble)
        self._show_toolbar_message(text, sender, model)
        QTimer.singleShot(50, lambda: self.scroll.verticalScrollBar().setValue(
            self.scroll.verticalScrollBar().maximum()
        ))

    def _register_interaction(self, entry: dict):
        if entry and entry.get("response"):
            self._last_jarvis_interaction = entry

    def _flag_last_answer(self):
        entry = self._last_jarvis_interaction
        if not entry:
            self._add_message("No recent Jarvis reply is available to flag yet.", "jarvis", "Eval")
            return

        issue, ok = QInputDialog.getText(
            self,
            "Flag Last Answer",
            "What's wrong with the last Jarvis answer?",
            text="Too generic for Aman."
        )
        if not ok or not issue.strip():
            return

        expected, ok = QInputDialog.getText(
            self,
            "Expected Behavior",
            "What should Jarvis have done instead?",
            text=""
        )
        if not ok:
            return

        failure = evals.log_failure(
            issue=issue.strip(),
            interaction_id=entry.get("id"),
            expected=expected.strip(),
            source="ui_feedback",
        )
        self._add_message(f"Logged feedback under {failure['category']} for the last Jarvis answer.", "jarvis", "Eval")

    def _set_status(self, text: str):
        self._status_label.setText(text)
        if "LISTEN" in text or "ACTIVE" in text or "VOICE" in text:
            self._status_label.setStyleSheet(f"color: {C_CYAN}; background: transparent; letter-spacing: 2px;")
            self._status_dot.set_color(C_CYAN)
            self._orb.set_state(JarvisOrb.STATE_LISTENING)
            self._signal_bars.set_intensity(0.55)
        elif "PROCESS" in text or "SCANNING" in text or "CAMERA" in text or "READING" in text:
            self._status_label.setStyleSheet(f"color: {C_WARNING}; background: transparent; letter-spacing: 2px;")
            self._status_dot.set_color(C_WARNING)
            self._orb.set_state(JarvisOrb.STATE_SPEAKING)
            self._signal_bars.set_intensity(1.0)
        else:
            self._status_label.setStyleSheet(f"color: {C_GREEN}; background: transparent; letter-spacing: 2px;")
            self._status_dot.set_color(C_GREEN)
            self._orb.set_state(JarvisOrb.STATE_IDLE)
            self._signal_bars.set_intensity(0.28)
        self._refresh_telemetry()

    def _send_text(self):
        text = self.input_field.text().strip()
        if not text:
            return
        self.input_field.clear()
        self._add_message(text, "user", "")

        if self._handle_memory(text):
            return

        worker = TextWorker(text)
        worker.message.connect(self._add_message)
        worker.interaction.connect(self._register_interaction)
        worker.status.connect(self._set_status)
        worker.start()
        self._workers.append(worker)

    def _attach_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Attach File", os.path.expanduser("~"),
            "All Files (*);;Text Files (*.txt);;Python (*.py);;Markdown (*.md)"
        )
        if not path:
            return
        content = terminal.read_file(path)
        filename = os.path.basename(path)
        self._add_message(f"📎 {filename}", "user", "")
        prompt = f"The user shared a file called '{filename}'. Here is its content:\n\n{content}\n\nAcknowledge it and ask what they want to do with it."
        worker = TextWorker(prompt)
        worker.message.connect(self._add_message)
        worker.interaction.connect(self._register_interaction)
        worker.status.connect(self._set_status)
        worker.start()
        self._workers.append(worker)

    def _handle_memory(self, text: str) -> bool:
        lower = text.lower().strip()
        if lower.startswith("remember "):
            fact = text[9:].strip()
            mem.add_fact(fact)
            self._add_message(f"Logged. I'll remember that {fact}.", "jarvis", "")
            return True
        if lower.startswith("forget "):
            keyword = text[7:].strip()
            removed = mem.forget(keyword)
            msg = f"Purged. All records of {keyword} removed." if removed else f"No records found for {keyword}."
            self._add_message(msg, "jarvis", "")
            return True
        if any(p in lower for p in ("switch to local", "use local mode", "go local")):
            msg = model_router.set_mode("local")
            self._add_message(msg, "jarvis", "")
            self._set_status("MODE: LOCAL")
            self._mode_lbl.setText("MODE: LOCAL")
            return True
        if any(p in lower for p in ("switch to cloud", "use cloud mode", "go cloud")):
            msg = model_router.set_mode("cloud")
            self._add_message(msg, "jarvis", "")
            self._set_status("MODE: CLOUD")
            self._mode_lbl.setText("MODE: CLOUD")
            return True
        if any(p in lower for p in ("switch to auto", "use auto mode", "auto mode")):
            msg = model_router.set_mode("auto")
            self._add_message(msg, "jarvis", "")
            self._set_status("MODE: AUTO")
            self._mode_lbl.setText("MODE: AUTO")
            return True
        if any(p in lower for p in ("what mode", "which mode", "what models", "local models")):
            from brain_ollama import list_local_models
            mode = model_router.get_mode()
            models = list_local_models()
            local_str = ", ".join(models) if models else "none pulled yet"
            msg = f"Currently in {mode} mode. Local models available: {local_str}."
            self._add_message(msg, "jarvis", "")
            return True
        if any(p in lower for p in ("restart yourself", "restart jarvis", "reload yourself", "apply changes")):
            self._add_message("Restarting systems now...", "jarvis", "")
            speak("Restarting to apply the latest changes.")
            QTimer.singleShot(1500, si.restart_jarvis)
            return True
        return False

    def closeEvent(self, event):
        if hasattr(self, "voice_worker"):
            self.voice_worker.stop()
        event.accept()


class OrbShellWindow(JarvisWindow):
    """Frameless orb-first shell that stays out of the way and reveals controls on demand."""

    def __init__(self):
        QMainWindow.__init__(self)
        self.setWindowTitle("J.A.R.V.I.S")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self._workers = []
        self._last_jarvis_interaction = None
        self._session_started = datetime.now()
        self._drag_pos = None
        self._tray_locked = False
        self._last_yield_ms = 0
        self._preferred_opacity = 0.84
        self._current_summary = "Awaiting input..."
        self._front_focus_app = ""
        self._front_focus_rect = None
        self._front_focus_checked_ms = 0
        self._front_focus_refreshing = False
        self._move_anim = None
        self._bootstrapped = False
        self._last_live_listener_started_at = 0.0
        self._last_live_transcript_at = 0.0
        self._last_live_suggestion_at = 0.0
        # Attributes from JarvisWindow.__init__ not inherited because
        # OrbShellWindow calls QMainWindow.__init__ directly (skips super)
        self._meeting_toolbar_mode = False
        self._meeting_toolbar_auto = False
        self._auto_listen_suppressed_meeting = None
        self._auto_listen_engaged_meeting = None
        self._normal_geometry = None
        self.resize(320, 340)
        self._build_ui()
        self._live_updates = LiveUpdateBridge(self)
        self._bind_live_update_signals()
        self._position_initial()
        self._adaptive_timer = QTimer(self)
        self._adaptive_timer.timeout.connect(self._adaptive_tick)

        self._call_status_timer = QTimer(self)
        self._call_status_timer.timeout.connect(self._refresh_live_call_status)

        self._collapse_timer = QTimer(self)
        self._collapse_timer.setSingleShot(True)
        self._collapse_timer.timeout.connect(lambda: self._set_tray_visible(False))

        self.setWindowOpacity(self._preferred_opacity)
        QTimer.singleShot(120, self._finish_startup)

    def _finish_startup(self):
        if self._bootstrapped:
            return
        self._bootstrapped = True
        self._refresh_live_call_status()
        self._bind_live_update_signals()
        self._start_voice()
        self._adaptive_timer.start(340)
        self._call_status_timer.start(1000)

    def _build_ui(self):
        central = QWidget()
        central.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        shell = QWidget()
        shell.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        shell.setStyleSheet("background: transparent;")
        self._shell = shell
        layout = QVBoxLayout(shell)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self._top_chip = QLabel("JARVIS ORBITAL SHELL")
        self._top_chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._top_chip.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._top_chip.setStyleSheet(
            _glass_panel_css(fill="rgba(3, 18, 28, 150)", radius=12) +
            f"color: {C_CYAN}; padding: 6px 12px; letter-spacing: 2px;"
        )
        self._top_chip.setFixedHeight(30)
        layout.addWidget(self._top_chip, alignment=Qt.AlignmentFlag.AlignCenter)

        orb_shell = QFrame()
        orb_shell.setObjectName("orb_shell")
        orb_shell.setStyleSheet(_glass_panel_css(fill="rgba(1, 10, 18, 100)", radius=120))
        orb_shell.setFixedSize(220, 220)
        orb_layout = QVBoxLayout(orb_shell)
        orb_layout.setContentsMargins(16, 12, 16, 12)
        orb_layout.setSpacing(2)
        orb_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._mode_lbl = QLabel(f"MODE {model_router.get_mode().upper()}")
        self._mode_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._mode_lbl.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        self._mode_lbl.setStyleSheet(f"color: {C_TEXT_DIM}; background: transparent; letter-spacing: 2px;")
        orb_layout.addWidget(self._mode_lbl)
        self._default_top_chip_text = "JARVIS ORBITAL SHELL"

        self._orb = JarvisOrb(size=164)
        orb_layout.addWidget(self._orb, alignment=Qt.AlignmentFlag.AlignCenter)

        self._status_row = QHBoxLayout()
        self._status_row.setSpacing(6)
        self._status_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_dot = PulseDot(C_GREEN)
        self._status_label = QLabel("ONLINE")
        self._status_label.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._status_label.setStyleSheet(f"color: {C_GREEN}; background: transparent; letter-spacing: 2px;")
        self._status_row.addWidget(self._status_dot)
        self._status_row.addWidget(self._status_label)
        status_wrap = QWidget()
        status_wrap.setStyleSheet("background: transparent;")
        status_wrap.setLayout(self._status_row)
        orb_layout.addWidget(status_wrap, alignment=Qt.AlignmentFlag.AlignCenter)

        layout.addWidget(orb_shell, alignment=Qt.AlignmentFlag.AlignCenter)

        self._signal_bars = SignalBars(bars=14)
        self._signal_bars.setFixedSize(180, 28)
        layout.addWidget(self._signal_bars, alignment=Qt.AlignmentFlag.AlignCenter)

        self._peek_label = QLabel("Ambient mode active. Hover or double-click for controls.")
        self._peek_label.setWordWrap(True)
        self._peek_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._peek_label.setFont(QFont("Courier New", 8))
        self._peek_label.setStyleSheet(
            _glass_panel_css(fill="rgba(3, 18, 28, 125)", radius=12) +
            f"color: {C_TEXT_DIM}; padding: 8px 10px;"
        )
        layout.addWidget(self._peek_label)

        self.suggest_panel = QFrame()
        self.suggest_panel.setStyleSheet(_glass_panel_css(fill="rgba(2, 16, 24, 220)", radius=14))
        tray = QVBoxLayout(self.suggest_panel)
        tray.setContentsMargins(12, 12, 12, 12)
        tray.setSpacing(8)

        tray_header = QHBoxLayout()
        tray_header.setSpacing(6)
        tray_header.addWidget(QLabel(""))
        hdr = QLabel("TACTICAL PANEL")
        hdr.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C_CYAN}; background: transparent; letter-spacing: 2px;")
        tray_header.addWidget(hdr)
        tray_header.addStretch()

        self._pin_btn = QPushButton("PIN")
        self._pin_btn.setFixedSize(44, 24)
        self._pin_btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        self._pin_btn.setStyleSheet(self._action_btn_css(C_TEXT_DIM))
        self._pin_btn.clicked.connect(self._toggle_tray_lock)
        tray_header.addWidget(self._pin_btn)
        tray.addLayout(tray_header)

        self.suggest_label = QTextEdit()
        self.suggest_label.setReadOnly(True)
        self.suggest_label.setPlainText("Awaiting input...")
        self.suggest_label.setMinimumHeight(110)
        self.suggest_label.setMaximumHeight(220)
        self.suggest_label.setFont(QFont("Courier New", 10))
        self.suggest_label.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.suggest_label.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.suggest_label.setStyleSheet(f"""
            QTextEdit {{
                background: rgba(2, 12, 20, 215);
                color: {C_TEXT};
                border: 1px solid {C_BORDER};
                border-radius: 10px;
                padding: 6px 8px;
            }}
        """)
        tray.addWidget(self.suggest_label)

        self.transcript_label = QLabel("No live transcript yet.")
        self.transcript_label.setWordWrap(True)
        self.transcript_label.setFont(QFont("Courier New", 8))
        self.transcript_label.setStyleSheet(f"color: {C_TEXT_DIM}; background: transparent;")
        tray.addWidget(self.transcript_label)

        self._call_status_label = QLabel("Meeting: scanning...\nAudio: checking route...\nPrivacy: checking...\nScreen scan: checking...")
        self._call_status_label.setWordWrap(True)
        self._call_status_label.setFont(QFont("Courier New", 8))
        self._call_status_label.setStyleSheet(
            _glass_panel_css(fill="rgba(3, 18, 28, 155)", radius=10) +
            f"color: {C_TEXT_DIM}; padding: 8px 10px;"
        )
        tray.addWidget(self._call_status_label)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.listen_btn = self._hud_btn("🎧")
        self.listen_btn.setToolTip("Toggle Smart Listen")
        self.listen_btn.clicked.connect(self._toggle_smart_listen)
        self.privacy_btn = self._hud_btn("🔇")
        self.privacy_btn.setToolTip("Toggle meeting-safe quiet mode")
        self.privacy_btn.clicked.connect(self._toggle_meeting_safe_mode)
        self.attach_btn = self._hud_btn("📎")
        self.attach_btn.setToolTip("Attach a file")
        self.attach_btn.clicked.connect(self._attach_file)
        self.flag_btn = self._hud_btn("⚑")
        self.flag_btn.setToolTip("Flag the last Jarvis answer")
        self.flag_btn.clicked.connect(self._flag_last_answer)
        self._scan_btn = self._hud_btn("⌁")
        self._scan_btn.setToolTip("Analyze the current screen")
        self._scan_btn.clicked.connect(self._hotkey_screen)
        action_row.addWidget(self.listen_btn)
        action_row.addWidget(self.privacy_btn)
        action_row.addWidget(self.attach_btn)
        action_row.addWidget(self.flag_btn)
        action_row.addWidget(self._scan_btn)
        tray.addLayout(action_row)

        input_row = QHBoxLayout()
        input_row.setSpacing(8)
        self.input_field = EnterLineEdit()
        self.input_field.setPlaceholderText("Type to Jarvis...")
        self.input_field.setFont(QFont("Courier New", 11))
        self.input_field.setStyleSheet(f"""
            QLineEdit {{
                background: rgba(0, 8, 14, 220);
                color: {C_TEXT};
                border: 1px solid {C_BORDER};
                border-radius: 10px;
                padding: 8px 12px;
                letter-spacing: 1px;
            }}
            QLineEdit:focus {{
                border: 1px solid {C_CYAN};
            }}
        """)
        self.input_field.returnPressed.connect(self._send_text)
        self.send_btn = QPushButton("SEND")
        self.send_btn.setFixedHeight(36)
        self.send_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self.send_btn.setStyleSheet(self._action_btn_css(C_CYAN))
        self.send_btn.clicked.connect(self._send_text)
        input_row.addWidget(self.input_field, stretch=1)
        input_row.addWidget(self.send_btn)
        tray.addLayout(input_row)

        self.suggest_panel.hide()
        layout.addWidget(self.suggest_panel)
        root.addWidget(shell)

        # Stub so parent-class methods that reference self.device_panel don't crash
        self.device_panel = QWidget()
        self.device_panel.hide()

    def _toggle_tray_lock(self):
        self._tray_locked = not self._tray_locked
        color = C_CYAN if self._tray_locked else C_TEXT_DIM
        self._pin_btn.setStyleSheet(self._action_btn_css(color))
        self._pin_btn.setText("LOCK" if self._tray_locked else "PIN")
        if self._tray_locked:
            self._set_tray_visible(True)
        else:
            self._collapse_timer.start(2200)

    def _screen_geometry_for(self, point: QPoint | None = None):
        point = point or QCursor.pos()
        screen = QApplication.screenAt(point)
        if screen is None:
            screen = QApplication.primaryScreen()
        return screen.availableGeometry()

    def _position_initial(self):
        geo = self._screen_geometry_for()
        self.adjustSize()
        x = geo.right() - self.width() - 22
        y = geo.center().y() - self.height() // 2
        self.move(max(geo.left() + 12, x), max(geo.top() + 12, y))

    def _candidate_positions(self, geo):
        margin = 18
        return [
            QPoint(geo.left() + margin, geo.top() + margin),
            QPoint(geo.right() - self.width() - margin, geo.top() + margin),
            QPoint(geo.left() + margin, geo.bottom() - self.height() - margin),
            QPoint(geo.right() - self.width() - margin, geo.bottom() - self.height() - margin),
            QPoint(geo.left() + margin, geo.center().y() - self.height() // 2),
            QPoint(geo.right() - self.width() - margin, geo.center().y() - self.height() // 2),
        ]

    def _animate_to(self, point: QPoint):
        if self.pos() == point:
            return
        self._move_anim = QPropertyAnimation(self, b"pos", self)
        self._move_anim.setDuration(240)
        self._move_anim.setStartValue(self.pos())
        self._move_anim.setEndValue(point)
        self._move_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._move_anim.start()

    def _dock_away_from(self, point: QPoint):
        geo = self._screen_geometry_for(point)
        current = self.pos()
        candidates = self._candidate_positions(geo)
        best = max(
            candidates,
            key=lambda pos: (
                (pos.x() - point.x()) ** 2 + (pos.y() - point.y()) ** 2,
                -abs(pos.x() - current.x()) - abs(pos.y() - current.y()),
            ),
        )
        self._animate_to(best)

    def _front_window_info(self, force: bool = False):
        now_ms = int(datetime.now().timestamp() * 1000)
        if not force and now_ms - self._front_focus_checked_ms < 1200:
            return self._front_focus_app, self._front_focus_rect

        if not self._front_focus_refreshing:
            self._front_focus_checked_ms = now_ms
            self._front_focus_refreshing = True
            threading.Thread(target=self._refresh_front_window_info, daemon=True).start()
        return self._front_focus_app, self._front_focus_rect

    def _refresh_front_window_info(self):
        script = r'''
tell application "System Events"
    try
        set frontProc to first application process whose frontmost is true
        set appName to name of frontProc
        try
            tell front window of frontProc
                set {xPos, yPos} to position
                set {wSize, hSize} to size
                return appName & "|" & xPos & "|" & yPos & "|" & wSize & "|" & hSize
            end tell
        on error
            return appName & "|NONE"
        end try
    on error
        return "NONE"
    end try
end tell
'''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=0.9,
            )
            raw = (result.stdout or "").strip()
            if not raw or raw == "NONE":
                self._front_focus_app = ""
                self._front_focus_rect = None
                return

            parts = raw.split("|")
            app_name = parts[0].strip()
            if len(parts) < 2 or parts[1] == "NONE":
                self._front_focus_app = app_name
                self._front_focus_rect = None
                return

            if len(parts) >= 5:
                x, y, w, h = [int(float(value.strip())) for value in parts[1:5]]
                rect = QRect(x, y, max(w, 1), max(h, 1))
                if app_name.lower() in {"python", "python3", "jarvis", "j.a.r.v.i.s"}:
                    rect = None
                self._front_focus_app = app_name
                self._front_focus_rect = rect
        except Exception:
            self._front_focus_app = ""
            self._front_focus_rect = None
        finally:
            self._front_focus_refreshing = False

    def _score_position_for_focus(self, pos: QPoint, focus_rect: QRect, screen_geo: QRect) -> float:
        shell_rect = QRect(pos, self.size())
        overlap = shell_rect.intersected(focus_rect)
        overlap_area = max(0, overlap.width()) * max(0, overlap.height())
        shell_area = max(1, shell_rect.width() * shell_rect.height())
        overlap_ratio = overlap_area / shell_area

        shell_center = shell_rect.center()
        focus_center = focus_rect.center()
        dx = shell_center.x() - focus_center.x()
        dy = shell_center.y() - focus_center.y()
        distance = math.sqrt(dx * dx + dy * dy)

        edge_clearance = min(
            abs(shell_rect.left() - screen_geo.left()),
            abs(shell_rect.right() - screen_geo.right()),
            abs(shell_rect.top() - screen_geo.top()),
            abs(shell_rect.bottom() - screen_geo.bottom()),
        )

        return distance - overlap_ratio * 2500 + edge_clearance * 0.2

    def _yield_from_focus_rect(self, focus_rect: QRect, app_name: str):
        screen_geo = self._screen_geometry_for(focus_rect.center())
        current = self.pos()
        candidates = self._candidate_positions(screen_geo)
        best = max(candidates, key=lambda pos: self._score_position_for_focus(pos, focus_rect, screen_geo))
        self._top_chip.setText(f"CLEAR VIEW FOR {app_name.upper()[:18]}" if app_name else "CLEAR VIEW MODE")
        if current != best:
            self._animate_to(best)

    def _set_tray_visible(self, visible: bool):
        if visible:
            self.suggest_panel.show()
            self._collapse_timer.stop()
        elif not self._tray_locked:
            self.suggest_panel.hide()
        self.adjustSize()

    def _action_btn_css(self, color: str) -> str:
        c = QColor(color)
        r, g, b = c.red(), c.green(), c.blue()
        return f"""
            QPushButton {{
                background: transparent;
                color: {color};
                border: 1px solid {color};
                border-radius: 10px;
                padding: 0 10px;
                letter-spacing: 1px;
            }}
            QPushButton:hover {{ background: rgba({r},{g},{b},0.14); }}
            QPushButton:pressed {{ background: rgba({r},{g},{b},0.28); }}
        """

    def _call_status_css(self, tone: str) -> str:
        palette = {
            "live": (C_GREEN, "rgba(0, 255, 136, 0.12)"),
            "meeting": (C_CYAN, "rgba(0, 212, 255, 0.12)"),
            "fallback": (C_WARNING, "rgba(255, 170, 0, 0.12)"),
            "idle": (C_TEXT_DIM, "rgba(3, 18, 28, 155)"),
        }
        border, fill = palette.get(tone, palette["idle"])
        return (
            _glass_panel_css(fill=fill, radius=10, border=border) +
            f"color: {border}; padding: 8px 10px;"
        )

    def _adaptive_tick(self):
        if self._drag_pos is not None:
            return

        cursor = QCursor.pos()
        now_ms = int(datetime.now().timestamp() * 1000)
        shell_rect = self.frameGeometry()
        padded = shell_rect.adjusted(-48, -48, 48, 48)
        app_name, focus_rect = self._front_window_info()

        if focus_rect is not None:
            overlap = shell_rect.intersected(focus_rect)
            overlap_area = max(0, overlap.width()) * max(0, overlap.height())
            shell_area = max(1, shell_rect.width() * shell_rect.height())
            overlap_ratio = overlap_area / shell_area
            focus_covers_screen = (
                focus_rect.width() * focus_rect.height() >
                self._screen_geometry_for(focus_rect.center()).width() *
                self._screen_geometry_for(focus_rect.center()).height() * 0.72
            )

            if overlap_ratio > 0.08 or focus_rect.contains(shell_rect.center()):
                if now_ms - self._last_yield_ms > 1000:
                    self._yield_from_focus_rect(focus_rect, app_name)
                    self._last_yield_ms = now_ms
                self.setWindowOpacity(0.24 if focus_covers_screen else 0.42)
                self._set_tray_visible(False)
                return

            if focus_rect.adjusted(-60, -60, 60, 60).contains(shell_rect.center()):
                self._top_chip.setText(f"NEAR ACTIVE WINDOW {app_name.upper()[:14]}" if app_name else "NEAR ACTIVE WINDOW")
                self.setWindowOpacity(0.54)
                if not self._tray_locked:
                    self._collapse_timer.start(1200)
                return

        self._top_chip.setText(self._default_top_chip_text)

        if shell_rect.contains(cursor):
            self.setWindowOpacity(0.96)
            self._set_tray_visible(True)
            self._collapse_timer.stop()
        elif padded.contains(cursor):
            self.setWindowOpacity(0.78)
            self._set_tray_visible(True)
            self._collapse_timer.start(2200)
        else:
            active = self._status_label.text() not in ("ONLINE", "STANDBY", "AWAITING WAKE WORD")
            target_opacity = 0.92 if active or self._tray_locked else self._preferred_opacity
            self.setWindowOpacity(target_opacity)
            if not self._tray_locked:
                self._collapse_timer.start(1400)

    def _add_message(self, text: str, sender: str, model: str):
        prefix = "YOU" if sender == "user" else "JARVIS"
        snippet = text.strip().replace("\n", " ")
        if len(snippet) > 220:
            snippet = snippet[:217].rstrip() + "..."
        event = "manual_prompt" if sender == "user" else "manual_response"
        _trace_ui_event(self, "orb-tray", event, text, model=model)
        self._current_summary = f"{prefix}: {snippet}"
        self._peek_label.setText(self._current_summary)
        if sender == "user":
            self.suggest_label.setPlainText(f"Working on: {snippet}")
            self.suggest_label.moveCursor(QTextCursor.MoveOperation.Start)
            self.transcript_label.setText("Generating response...")
            self._set_tray_visible(True)
        else:
            self.suggest_label.setPlainText(text)
            self.suggest_label.moveCursor(QTextCursor.MoveOperation.Start)
            self._set_tray_visible(True)
            self._collapse_timer.start(4200)

    def _refresh_live_call_status(self):
        meeting = _overlay_mod.detect_meeting_app() or "NONE"
        snapshot = _meeting_status_snapshot()
        live = _live_listener_snapshot(snapshot)
        privacy = call_privacy.snapshot()
        preferred = live.get("preferred", {}) or snapshot.get("preferred_source", {}) or snapshot.get("preferred", {})
        listener_started_at = float(live.get("started_at") or snapshot.get("started_at") or 0.0)
        if listener_started_at > self._last_live_listener_started_at:
            self._last_live_listener_started_at = listener_started_at
            self._last_live_transcript_at = 0.0
            self._last_live_suggestion_at = 0.0
        last_transcript = (live.get("last_transcript") or snapshot.get("last_transcript") or "").strip()
        last_suggestion = (live.get("last_suggestion") or snapshot.get("last_suggestion") or "").strip()
        last_transcript_at = float(live.get("last_transcript_at") or snapshot.get("last_transcript_at") or 0.0)
        last_suggestion_at = float(live.get("last_suggestion_at") or snapshot.get("last_suggestion_at") or 0.0)
        device_name = live.get("active_device_name") or snapshot.get("active_device_name") or preferred.get("device_name") or "unknown"
        scan_ready = "READY" if shutil.which("screencapture") else "UNAVAILABLE"

        if last_transcript and last_transcript_at > self._last_live_transcript_at:
            self._last_live_transcript_at = last_transcript_at
            self._apply_live_transcript_update(last_transcript)
        if last_suggestion and last_suggestion_at > self._last_live_suggestion_at:
            self._last_live_suggestion_at = last_suggestion_at
            self._apply_live_suggestion_update(last_suggestion)

        if bool(live.get("running", snapshot.get("running", False))):
            audio_line = f"Audio: live via {device_name}"
            tone = "live"
            self.listen_btn.setText("■")
            self._top_chip.setText("SMART LISTEN ACTIVE")
        elif preferred.get("kind") == "microphone":
            audio_line = f"Audio: mic fallback {device_name}"
            tone = "fallback"
            self.listen_btn.setText("🎧")
        elif meeting != "NONE":
            audio_line = f"Audio: ready via {device_name}"
            tone = "meeting"
            self.listen_btn.setText("🎧")
        else:
            audio_line = f"Audio: ready via {device_name}"
            tone = "idle"
            self.listen_btn.setText("🎧")

        meeting_line = f"Meeting: {meeting}" if meeting != "NONE" else "Meeting: no active call detected"
        if privacy.get("suppressing_audio"):
            privacy_line = "Privacy: quiet mode active for live call"
            self.privacy_btn.setStyleSheet(self._action_btn_css(C_GREEN))
        elif privacy.get("enabled"):
            privacy_line = "Privacy: quiet mode armed"
            self.privacy_btn.setStyleSheet(self._action_btn_css(C_CYAN))
        else:
            privacy_line = "Privacy: quiet mode off"
            self.privacy_btn.setStyleSheet(self._action_btn_css(C_TEXT_DIM))
        self._call_status_label.setText(
            f"{meeting_line}\n{audio_line}\n{privacy_line}\nScreen scan: {scan_ready}"
        )
        self._call_status_label.setStyleSheet(self._call_status_css(tone))

    def _toggle_meeting_safe_mode(self):
        enabled = call_privacy.toggle_enabled()
        msg = call_privacy.status_text()
        self._add_message(msg, "jarvis", "Meeting")
        self._set_status("ONLINE")
        self._refresh_live_call_status()

    def _toggle_smart_listen(self):
        if _meeting_is_running():
            msg = _meeting_stop()
            self._add_message(msg, "jarvis", "")
            self._last_live_listener_started_at = 0.0
            self._last_live_transcript_at = 0.0
            self._last_live_suggestion_at = 0.0
            self.transcript_label.setText("Smart Listen offline.")
            self.listen_btn.setText("🎧")
            self._set_status("ONLINE")
        else:
            msg = _meeting_start(
                on_transcript=self._on_transcript,
                on_suggestion=self._on_suggestion,
            )
            self._add_message(msg, "jarvis", "")
            self._last_live_listener_started_at = 0.0
            self._last_live_transcript_at = 0.0
            self._last_live_suggestion_at = 0.0
            self.transcript_label.setText("Live call transcript incoming...")
            self.listen_btn.setText("■")
            self._set_status("LISTENING")
            self._set_tray_visible(True)
        self._refresh_live_call_status()

    def _on_transcript(self, text: str):
        self._live_updates.transcript.emit(text)

    def _apply_live_transcript_update(self, text: str):
        _trace_ui_event(self, "orb-tray", "live_transcript", text)
        preview = text.strip().replace("\n", " ")
        if len(preview) > 180:
            preview = preview[:177].rstrip() + "..."
        _force_text_widget_update(self.transcript_label, f"Transcript: {text[:240]}")
        self._current_summary = f"TRANSCRIPT: {preview}"
        self._peek_label.setText(self._current_summary)
        self._top_chip.setText("SMART LISTEN ACTIVE")
        self._set_tray_visible(True)

    def _show_suggestion(self, suggestion: str):
        self._apply_live_suggestion_update(suggestion)

    def _apply_live_suggestion_update(self, suggestion: str):
        _trace_ui_event(self, "orb-tray", "live_suggestion", suggestion)
        preview = suggestion.strip().replace("\n", " ")
        if len(preview) > 180:
            preview = preview[:177].rstrip() + "..."
        _force_text_widget_update(self.suggest_label, suggestion)
        self.suggest_label.moveCursor(QTextCursor.MoveOperation.Start)
        if suggestion:
            self._add_message(suggestion, "jarvis", "Meeting")
        self._current_summary = f"SUGGESTION: {preview}"
        self._peek_label.setText(self._current_summary)
        self._top_chip.setText("SMART LISTEN ACTIVE")
        self.transcript_label.setText("Live suggestion ready.")
        self._set_tray_visible(True)

    def _on_suggestion(self, suggestion: str):
        self._live_updates.suggestion.emit(suggestion)

    def mouseDoubleClickEvent(self, event):
        self._set_tray_visible(not self.suggest_panel.isVisible())
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() == Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(120, lambda: stealth.apply_stealth(int(self.winId())))


class EnterLineEdit(QLineEdit):
    pass


# ── Entry point ────────────────────────────────────────────────────────────────

def run():
    app = QApplication(sys.argv)
    runtime_icon = _build_runtime_app_icon()
    _apply_macos_identity(app, runtime_icon)
    app.setStyle("Fusion")

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window,          QColor(C_BG))
    palette.setColor(QPalette.ColorRole.WindowText,      QColor(C_TEXT))
    palette.setColor(QPalette.ColorRole.Base,            QColor(C_BG2))
    palette.setColor(QPalette.ColorRole.AlternateBase,   QColor(C_PANEL))
    palette.setColor(QPalette.ColorRole.Text,            QColor(C_TEXT))
    palette.setColor(QPalette.ColorRole.Button,          QColor(C_BG2))
    palette.setColor(QPalette.ColorRole.ButtonText,      QColor(C_CYAN))
    palette.setColor(QPalette.ColorRole.Highlight,       QColor(C_CYAN))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(C_BG))
    app.setPalette(palette)
    app.setStyleSheet(f"""
        QMainWindow, QWidget {{
            color: {C_TEXT};
            background: transparent;
        }}
        QToolTip {{
            color: {C_TEXT};
            background-color: rgba(2, 16, 24, 235);
            border: 1px solid {C_CYAN};
            padding: 6px 8px;
            font-family: "Courier New";
        }}
        QScrollBar:horizontal {{
            height: 0px;
        }}
    """)

    bundled_launch = bool(getattr(sys, "frozen", False)) or os.getenv("JARVIS_BUNDLED_APP", "").lower() in {"1", "true", "yes", "on"}
    default_classic = sys.platform == "darwin" and bundled_launch
    use_classic = (
        "--classic-ui" in sys.argv
        or os.getenv("JARVIS_UI_SHELL", "").lower() == "classic"
        or default_classic
    )
    window = JarvisWindow() if use_classic else OrbShellWindow()
    window.setWindowIcon(runtime_icon)
    window.show()
    QTimer.singleShot(0, lambda: _activate_macos_app(window))
    sys.exit(app.exec())


if __name__ == "__main__":
    run()
