import sys
import re
import threading
import os
import math
import random
import learner
import model_router
import self_improve as si
import hotkeys
import meeting_listener
import agents
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QTextEdit, QLineEdit, QLabel, QFileDialog,
    QScrollArea, QFrame, QSizePolicy, QGraphicsDropShadowEffect
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QSize, QPropertyAnimation,
    QRect, QPoint, QEasingCurve, QRectF
)
from PyQt6.QtGui import (
    QFont, QColor, QPalette, QIcon, QTextCursor, QKeyEvent,
    QPainter, QPen, QBrush, QLinearGradient, QRadialGradient,
    QPainterPath, QFontDatabase
)

from router import route_stream, set_timer_callback
from voice import speak, speak_stream, listen, wait_for_wake_word
from brain import ask as ask_gpt
import memory as mem
import briefing
import tools
import google_services as gs
import terminal
import stealth
import overlay as _overlay_mod

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

        # Dot grid
        grid_pen = QPen(QColor(0, 180, 220, 18))
        p.setPen(grid_pen)
        step = 28
        for x in range(0, w, step):
            for y in range(0, h, step):
                p.drawPoint(x, y)

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


# ── Worker threads ─────────────────────────────────────────────────────────────

class VoiceWorker(QThread):
    message = pyqtSignal(str, str, str)
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
            speak(response)
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
        transcript = "\n".join(exchanges[-10:])
        summary = ask_gpt(f"Summarize this Jarvis conversation in one sentence:\n{transcript}")
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
                color: #FFD0A0;
                background: #1A0A00;
                border: 1px solid #FF6B00;
                border-radius: 2px;
                padding: 8px 12px;
            """)
            layout.setAlignment(Qt.AlignmentFlag.AlignRight)
            self.setStyleSheet("background: transparent;")
        else:
            msg.setStyleSheet(f"""
                color: {C_TEXT};
                background: {C_PANEL};
                border: 1px solid {C_BORDER};
                border-radius: 2px;
                padding: 8px 12px;
            """)
            layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
            self.setStyleSheet("background: transparent;")
            _glow(msg, C_CYAN, 8)

        layout.addWidget(msg)


# ── Main Window ────────────────────────────────────────────────────────────────

class JarvisWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("J.A.R.V.I.S")
        self.setMinimumSize(460, 900)
        self.resize(500, 980)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self._workers = []
        self._build_ui()
        self._start_voice()

    # ── Build UI ───────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Root widget
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
        header.setFixedHeight(70)
        header.setAutoFillBackground(True)
        hp = header.palette()
        hp.setColor(QPalette.ColorRole.Window, QColor(C_BG2))
        header.setPalette(hp)
        header.setStyleSheet(f"border-bottom: 1px solid {C_BORDER};")

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
        overlay_btn = QPushButton("⬡ ASSIST")
        overlay_btn.setFixedHeight(28)
        overlay_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        overlay_btn.setStyleSheet(f"""
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
        overlay_btn.setToolTip("Toggle Meeting Assist overlay (Cmd+Shift+O)")
        overlay_btn.clicked.connect(_overlay_mod.toggle)
        h_layout.addWidget(overlay_btn)

        root.addWidget(header)

        # ── Divider ──────────────────────────────────────────────────────────
        div = QLabel()
        div.setFixedHeight(1)
        div.setStyleSheet(f"background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 transparent, stop:0.3 {C_CYAN}, stop:0.7 {C_CYAN}, stop:1 transparent);")
        root.addWidget(div)

        # ── Orb panel ────────────────────────────────────────────────────────
        orb_panel = QWidget()
        orb_panel.setFixedHeight(290)
        orb_panel.setStyleSheet("background: transparent;")
        orb_layout = QVBoxLayout(orb_panel)
        orb_layout.setContentsMargins(0, 8, 0, 8)
        orb_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._orb = JarvisOrb(size=260)
        orb_layout.addWidget(self._orb, alignment=Qt.AlignmentFlag.AlignCenter)
        root.addWidget(orb_panel)

        # Divider below orb
        div1b = QLabel()
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
        div2.setFixedHeight(1)
        div2.setStyleSheet(div.styleSheet())
        root.addWidget(div2)

        # ── Input area ───────────────────────────────────────────────────────
        input_bar = QWidget()
        input_bar.setFixedHeight(68)
        input_bar.setAutoFillBackground(True)
        ip = input_bar.palette()
        ip.setColor(QPalette.ColorRole.Window, QColor(C_BG2))
        input_bar.setPalette(ip)

        i_layout = QHBoxLayout(input_bar)
        i_layout.setContentsMargins(12, 10, 12, 10)
        i_layout.setSpacing(8)

        self.attach_btn = self._hud_btn("📎")
        self.attach_btn.setToolTip("Attach a file")
        self.attach_btn.clicked.connect(self._attach_file)

        self.listen_btn = self._hud_btn("🎧")
        self.listen_btn.setToolTip("Smart Listen — tap into call audio (Cmd+Shift+M)")
        self.listen_btn.clicked.connect(self._toggle_smart_listen)

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
        i_layout.addWidget(self.input_field, stretch=1)
        i_layout.addWidget(self.send_btn)
        root.addWidget(input_bar)

        # ── Smart Listen panel ───────────────────────────────────────────────
        self.suggest_panel = QWidget()
        self.suggest_panel.setAutoFillBackground(True)
        sp = self.suggest_panel.palette()
        sp.setColor(QPalette.ColorRole.Window, QColor("#010D18"))
        self.suggest_panel.setPalette(sp)
        self.suggest_panel.setStyleSheet(f"border-top: 1px solid {C_BORDER};")
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

    # ── Resize: keep HUD background covering full window ──────────────────────

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_hud_bg"):
            self._hud_bg.setGeometry(0, 0, self.width(), self.height())

    # ── Voice & startup ────────────────────────────────────────────────────────

    def _start_voice(self):
        set_timer_callback(self._on_timer_done)
        learner.start_background_feed()

        # Start proactive agents
        agents.start(on_alert=self._on_agent_alert)

        threading.Thread(target=self._maybe_brief, daemon=True).start()

        self.voice_worker = VoiceWorker()
        self.voice_worker.message.connect(self._add_message)
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
        if meeting_listener.is_running():
            msg = meeting_listener.stop()
            self.suggest_panel.hide()
            self._add_message(msg, "jarvis", "")
        else:
            msg = meeting_listener.start(
                on_transcript=self._on_transcript,
                on_suggestion=self._on_suggestion,
            )
            self.suggest_panel.show()
            self._add_message(msg, "jarvis", "")

    def _on_transcript(self, text: str):
        QTimer.singleShot(0, lambda: self.transcript_label.setText(f'» {text}'))

    def _on_suggestion(self, suggestion: str):
        QTimer.singleShot(0, lambda: self._show_suggestion(suggestion))

    def _show_suggestion(self, suggestion: str):
        self.suggest_label.setText(suggestion)
        self.suggest_panel.show()

    # ── Briefing ───────────────────────────────────────────────────────────────

    def _maybe_brief(self):
        facts = mem.list_facts()
        learner.reflect()
        if briefing.should_brief():
            try:
                speak(briefing.build_briefing(facts))
                speak(f"Weather: {tools.get_weather()}")
                speak(gs.get_todays_events())
                speak(gs.get_unread_emails(max_results=3))
                from learner import _load_knowledge
                feed = _load_knowledge().get("knowledge_feed", [])
                if feed:
                    item = feed[0]
                    speak(f"Something you might find relevant — {item['summary']}")
                self._add_message("Morning briefing delivered.", "jarvis", "")
            except Exception as e:
                print(f"[Briefing Error] {e}")
        self._set_status("ONLINE")

    def _sync_orb_to_voice(self):
        """Keep orb state in sync with actual TTS playback."""
        if hasattr(self, "_voice_event"):
            speaking = not self._voice_event.is_set()  # cleared = speaking
            if speaking:
                self._orb.set_state(JarvisOrb.STATE_SPEAKING)

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

    def _add_message(self, text: str, sender: str, model: str):
        bubble = MessageBubble(text, sender, model)
        count = self.chat_layout.count()
        self.chat_layout.insertWidget(count - 1, bubble)
        QTimer.singleShot(50, lambda: self.scroll.verticalScrollBar().setValue(
            self.scroll.verticalScrollBar().maximum()
        ))

    def _set_status(self, text: str):
        self._status_label.setText(text)
        if "LISTEN" in text or "ACTIVE" in text or "VOICE" in text:
            self._status_label.setStyleSheet(f"color: {C_CYAN}; background: transparent; letter-spacing: 2px;")
            self._status_dot.set_color(C_CYAN)
            self._orb.set_state(JarvisOrb.STATE_LISTENING)
        elif "PROCESS" in text or "SCANNING" in text or "CAMERA" in text or "READING" in text:
            self._status_label.setStyleSheet(f"color: #FFAA00; background: transparent; letter-spacing: 2px;")
            self._status_dot.set_color("#FFAA00")
            self._orb.set_state(JarvisOrb.STATE_SPEAKING)
        else:
            self._status_label.setStyleSheet(f"color: {C_GREEN}; background: transparent; letter-spacing: 2px;")
            self._status_dot.set_color(C_GREEN)
            self._orb.set_state(JarvisOrb.STATE_IDLE)

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


class EnterLineEdit(QLineEdit):
    pass


# ── Entry point ────────────────────────────────────────────────────────────────

def run():
    app = QApplication(sys.argv)
    app.setApplicationName("J.A.R.V.I.S")
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

    window = JarvisWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    run()
