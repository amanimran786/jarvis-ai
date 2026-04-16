"""
Jarvis Meeting Overlay — floating HUD toolbar for live calls.

Invisible to screen share (NSWindowSharingNone).
Sits at the bottom of the screen, always on top.

Features:
  - Auto-detects active meeting app (Zoom, Teams, Meet, FaceTime)
  - Live rolling transcript feed from call audio
  - Real-time AI suggestion panel (answers questions, solves code)
  - Scan Screen button — screenshots and answers anything visible
  - Copy / Speak buttons for each suggestion
  - Toggle: Cmd+Shift+O
"""

import subprocess
import threading
import os
import sys
import time
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFrame, QSizePolicy, QScrollArea,
    QGraphicsDropShadowEffect
)
from PyQt6.QtCore import Qt, QTimer, QPoint, pyqtSignal, QThread, QRectF, QMetaObject, pyqtSlot
from PyQt6.QtGui import (
    QFont, QColor, QPalette, QPainter, QPen, QBrush,
    QLinearGradient, QCursor, QScreen
)

import meeting_listener
import camera
import stealth
from voice import speak
from brains.brain_claude import ask_claude
from config import SONNET

# ── Palette (matches main HUD) ────────────────────────────────────────────────
C_BG       = "#020A10"
C_BG2      = "#030D14"
C_CYAN     = "#00D4FF"
C_BORDER   = "#0D4F70"
C_TEXT     = "#A8E6FF"
C_TEXT_DIM = "#4A8FA8"
C_ORANGE   = "#FF6B00"
C_GREEN    = "#00FF88"
C_AMBER    = "#FFAA00"
C_PANEL    = "#021018"
C_RED      = "#FF4444"

# Meeting apps to detect
MEETING_APPS = {
    "zoom":          "ZOOM",
    "teams":         "TEAMS",
    "msteams":       "TEAMS",
    "microsoft teams": "TEAMS",
    "microsoft teams webview": "TEAMS",
    "teams webview": "TEAMS",
    "meet":          "MEET",
    "facetime":      "FACETIME",
    "webex":         "WEBEX",
    "slack":         "SLACK",
    "discord":       "DISCORD",
    "skype":         "SKYPE",
    "bluejeans":     "BLUEJEANS",
    "gotomeeting":   "GOTOMEETING",
}

MEETING_URL_PATTERNS = {
    "meet.google.com": "MEET",
    "teams.microsoft.com": "TEAMS",
    "teams.live.com": "TEAMS",
    "teams.cloud.microsoft": "TEAMS",
    "teams.microsoft365.com": "TEAMS",
    "app.zoom.us": "ZOOM",
    "zoom.us/wc": "ZOOM",
    "zoom.us/j/": "ZOOM",
    "webex.com": "WEBEX",
}


def meeting_label_for_url(url: str) -> str | None:
    low = (url or "").strip().lower()
    for pattern, label in MEETING_URL_PATTERNS.items():
        if pattern in low:
            return label
    return None


def _running_app_names() -> set[str]:
    try:
        result = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of every process whose background only is false'],
            capture_output=True, text=True, timeout=3
        )
        raw = (result.stdout or "").strip()
        return {name.strip() for name in raw.split(",") if name.strip()}
    except Exception:
        return set()


def _frontmost_app_name() -> str:
    try:
        result = subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to get name of first application process whose frontmost is true'],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        return (result.stdout or "").strip()
    except Exception:
        return ""


def _all_process_names() -> set[str]:
    try:
        result = subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to get name of every process'],
            capture_output=True,
            text=True,
            timeout=3,
        )
        raw = (result.stdout or "").strip()
        return {name.strip() for name in raw.split(",") if name.strip()}
    except Exception:
        return set()


def _browser_active_meeting_label(app_name: str, url_script: str) -> str | None:
    try:
        result = subprocess.run(
            ["osascript", "-e", url_script],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return meeting_label_for_url((result.stdout or "").strip().lower())
    except Exception:
        return None


def _browser_any_meeting_label(app_name: str) -> str | None:
    script = f'''
    tell application "{app_name}"
        set outText to ""
        repeat with w from 1 to count of windows
            repeat with t from 1 to count of tabs of window w
                try
                    set theURL to URL of tab t of window w
                    set outText to outText & theURL & linefeed
                end try
            end repeat
        end repeat
        return outText
    end tell
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=4,
        )
        urls = (result.stdout or "").splitlines()
        for url in urls:
            label = meeting_label_for_url(url)
            if label:
                return label
    except Exception:
        return None
    return None

_instance = None  # singleton overlay reference
_meeting_cache_lock = threading.Lock()
_meeting_cache_value = None
_meeting_cache_until = 0.0
_meeting_refresh_in_flight = False


# ── Meeting detection ─────────────────────────────────────────────────────────

def _browser_meeting_detection_enabled() -> bool:
    return os.getenv("JARVIS_BROWSER_MEETING_DETECTION", "").strip().lower() in {"1", "true", "yes", "on"}

def _compute_meeting_app() -> str | None:
    running_names = _running_app_names()
    process_names = running_names | _all_process_names()
    running = ", ".join(sorted(process_names)).lower()
    for proc, label in MEETING_APPS.items():
        if proc in running:
            return label

    if not _browser_meeting_detection_enabled():
        return None

    browser_checks = {
        "Google Chrome": 'tell application "Google Chrome" to get URL of active tab of front window',
        "Safari": 'tell application "Safari" to get URL of current tab of front window',
        "Brave Browser": 'tell application "Brave Browser" to get URL of active tab of front window',
        "ChatGPT Atlas": 'tell application "ChatGPT Atlas" to get URL of active tab of front window',
    }
    frontmost = _frontmost_app_name()
    script = browser_checks.get(frontmost)
    if frontmost and script and frontmost in running_names:
        label = _browser_active_meeting_label(frontmost, script)
        if label:
            return label
        label = _browser_any_meeting_label(frontmost)
        if label:
            return label
    return None


def _refresh_meeting_cache_async() -> None:
    global _meeting_refresh_in_flight, _meeting_cache_value, _meeting_cache_until
    with _meeting_cache_lock:
        if _meeting_refresh_in_flight:
            return
        _meeting_refresh_in_flight = True

    def _worker():
        global _meeting_refresh_in_flight, _meeting_cache_value, _meeting_cache_until
        try:
            value = _compute_meeting_app()
            with _meeting_cache_lock:
                _meeting_cache_value = value
                _meeting_cache_until = time.monotonic() + 3.0
        finally:
            with _meeting_cache_lock:
                _meeting_refresh_in_flight = False

    threading.Thread(target=_worker, daemon=True).start()


def detect_meeting_app(force_refresh: bool = False) -> str | None:
    """Return the best-known active meeting app label without blocking the UI by default."""
    global _meeting_cache_value, _meeting_cache_until
    now = time.monotonic()
    with _meeting_cache_lock:
        cached_value = _meeting_cache_value
        cached_until = _meeting_cache_until

    if force_refresh:
        value = _compute_meeting_app()
        with _meeting_cache_lock:
            _meeting_cache_value = value
            _meeting_cache_until = time.monotonic() + 3.0
        return value

    if now < cached_until:
        return cached_value

    _refresh_meeting_cache_async()
    return cached_value


# ── Screen analysis worker ────────────────────────────────────────────────────

class ScreenAnalysisWorker(QThread):
    """Takes a screenshot and generates a detailed technical answer."""
    result = pyqtSignal(str)
    status = pyqtSignal(str)

    def run(self):
        self.status.emit("SCANNING SCREEN...")
        try:
            prompt = camera._engineering_vision_prompt(
                (
                    "You are Jarvis, an AI assistant helping Aman during a live interview or technical call. "
                    "Analyze everything visible on the screen RIGHT NOW and provide the most useful response:\n\n"
                    "PRIORITY ORDER:\n"
                    "1. Coding problem / LeetCode / algorithm question → give the full working solution with time/space complexity\n"
                    "2. System design question → outline the architecture clearly\n"
                    "3. Technical interview question → give a strong, precise answer with an example\n"
                    "4. Code with a bug → identify the bug and provide the fix\n"
                    "5. Presentation / document → key talking points Aman should mention\n"
                    "6. Anything else → describe what's visible and provide relevant context\n\n"
                    "Be direct. No markdown formatting. Write as if speaking out loud. "
                    "For code, include the actual code block. Max 6 sentences for explanations."
                ),
                force=True,
            )
            answer = camera.screenshot_and_describe(prompt)
            self.result.emit(answer)
        except Exception as e:
            self.result.emit(f"Screen scan failed: {e}")
        self.status.emit("LIVE")


# ── Animated pulse dot ────────────────────────────────────────────────────────

class PulseDot(QWidget):
    def __init__(self, color=C_GREEN, size=8, parent=None):
        super().__init__(parent)
        self._sz = size
        self.setFixedSize(size + 4, size + 4)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._color = color
        self._alpha = 255
        self._dir = -5
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(30)

    def set_color(self, c):
        self._color = c

    def _tick(self):
        self._alpha += self._dir
        if self._alpha <= 60: self._dir = 5
        elif self._alpha >= 255: self._dir = -5
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QColor(self._color)
        c.setAlpha(self._alpha)
        p.setBrush(QBrush(c))
        p.setPen(Qt.PenStyle.NoPen)
        off = (self.width() - self._sz) // 2
        p.drawEllipse(off, off, self._sz, self._sz)
        p.end()


# ── Transcript line ────────────────────────────────────────────────────────────

class TranscriptLine(QLabel):
    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setFont(QFont("Courier New", 10))
        self.setWordWrap(True)
        self.setStyleSheet(f"""
            color: {C_TEXT_DIM};
            background: transparent;
            padding: 1px 4px;
            border-left: 2px solid {C_BORDER};
        """)


# ── Main overlay window ────────────────────────────────────────────────────────

class MeetingOverlay(QMainWindow):
    _transcript_sig = pyqtSignal(str)
    _suggestion_sig = pyqtSignal(str)
    _status_sig     = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Jarvis Assist")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        self._drag_pos = None
        self._listening = False
        self._current_suggestion = ""
        self._scan_worker = None

        self._build_ui()
        self._position_bottom()

        # Wire signals (safe cross-thread UI updates)
        self._transcript_sig.connect(self._on_transcript)
        self._suggestion_sig.connect(self._on_suggestion)
        self._status_sig.connect(self._set_status)

        # Meeting app detection — poll every 5s
        self._meet_timer = QTimer(self)
        self._meet_timer.timeout.connect(self._check_meeting)
        self._meet_timer.start(5000)
        self._check_meeting()

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QWidget()
        root.setObjectName("root")
        root.setStyleSheet(f"""
            #root {{
                background: {C_BG};
                border: 1px solid {C_BORDER};
                border-radius: 6px;
            }}
        """)
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Title bar ──────────────────────────────────────────────────────
        title_bar = QWidget()
        title_bar.setFixedHeight(34)
        title_bar.setStyleSheet(f"""
            background: {C_BG2};
            border-bottom: 1px solid {C_BORDER};
            border-top-left-radius: 6px;
            border-top-right-radius: 6px;
        """)
        tb = QHBoxLayout(title_bar)
        tb.setContentsMargins(10, 0, 8, 0)
        tb.setSpacing(8)

        # Arc dot
        self._status_dot = PulseDot(C_GREEN, 7)
        tb.addWidget(self._status_dot)

        # Title
        title = QLabel("J.A.R.V.I.S  ASSIST")
        title.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C_CYAN}; background: transparent; letter-spacing: 3px;")
        tb.addWidget(title)

        tb.addStretch()

        # Meeting badge
        self._meet_badge = QLabel("NO MEETING DETECTED")
        self._meet_badge.setFont(QFont("Courier New", 8))
        self._meet_badge.setStyleSheet(f"""
            color: {C_TEXT_DIM};
            background: transparent;
            padding: 2px 6px;
            border: 1px solid {C_BORDER};
            border-radius: 3px;
        """)
        tb.addWidget(self._meet_badge)

        # Status label
        self._status_lbl = QLabel("STANDBY")
        self._status_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._status_lbl.setStyleSheet(f"color: {C_TEXT_DIM}; background: transparent; letter-spacing: 1px;")
        tb.addWidget(self._status_lbl)

        # Close / minimise
        hide_btn = QPushButton("—")
        hide_btn.setFixedSize(22, 22)
        hide_btn.setFont(QFont("Courier New", 10))
        hide_btn.setStyleSheet(self._icon_btn_css())
        hide_btn.clicked.connect(self.hide)
        tb.addWidget(hide_btn)

        layout.addWidget(title_bar)

        # ── Transcript strip ───────────────────────────────────────────────
        self._transcript_scroll = QScrollArea()
        self._transcript_scroll.setFixedHeight(56)
        self._transcript_scroll.setWidgetResizable(True)
        self._transcript_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._transcript_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._transcript_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._transcript_scroll.setStyleSheet(f"""
            background: {C_PANEL};
            border-bottom: 1px solid {C_BORDER};
        """)

        self._transcript_widget = QWidget()
        self._transcript_widget.setStyleSheet("background: transparent;")
        self._transcript_layout = QVBoxLayout(self._transcript_widget)
        self._transcript_layout.setContentsMargins(6, 4, 6, 4)
        self._transcript_layout.setSpacing(2)
        self._transcript_layout.addStretch()

        placeholder = QLabel("Transcript will appear here when listening...")
        placeholder.setFont(QFont("Courier New", 9))
        placeholder.setStyleSheet(f"color: {C_TEXT_DIM}; background: transparent; padding: 2px 4px;")
        self._transcript_layout.insertWidget(0, placeholder)
        self._placeholder = placeholder

        self._transcript_scroll.setWidget(self._transcript_widget)
        layout.addWidget(self._transcript_scroll)

        # ── Suggestion area ────────────────────────────────────────────────
        suggest_wrap = QWidget()
        suggest_wrap.setStyleSheet(f"background: {C_BG};")
        sw = QVBoxLayout(suggest_wrap)
        sw.setContentsMargins(10, 8, 10, 4)
        sw.setSpacing(4)

        suggest_header = QLabel("◈  JARVIS SUGGESTION")
        suggest_header.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        suggest_header.setStyleSheet(f"color: {C_CYAN}; background: transparent; letter-spacing: 2px;")
        sw.addWidget(suggest_header)

        self._suggestion_lbl = QLabel("Awaiting input...")
        self._suggestion_lbl.setWordWrap(True)
        self._suggestion_lbl.setFont(QFont("Courier New", 11))
        self._suggestion_lbl.setMinimumHeight(60)
        self._suggestion_lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._suggestion_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._suggestion_lbl.setStyleSheet(f"""
            color: {C_TEXT};
            background: {C_PANEL};
            border: 1px solid {C_BORDER};
            border-radius: 3px;
            padding: 8px 10px;
        """)
        sw.addWidget(self._suggestion_lbl)

        layout.addWidget(suggest_wrap)

        # ── Action buttons ─────────────────────────────────────────────────
        action_bar = QWidget()
        action_bar.setFixedHeight(48)
        action_bar.setStyleSheet(f"""
            background: {C_BG2};
            border-top: 1px solid {C_BORDER};
            border-bottom-left-radius: 6px;
            border-bottom-right-radius: 6px;
        """)
        ab = QHBoxLayout(action_bar)
        ab.setContentsMargins(10, 6, 10, 6)
        ab.setSpacing(8)

        self._scan_btn = self._action_btn("📸  SCAN SCREEN", C_CYAN)
        self._scan_btn.clicked.connect(self._scan_screen)
        ab.addWidget(self._scan_btn)

        self._listen_btn = self._action_btn("🎧  START LISTEN", C_GREEN)
        self._listen_btn.clicked.connect(self._toggle_listen)
        ab.addWidget(self._listen_btn)

        self._copy_btn = self._action_btn("📋  COPY", C_AMBER)
        self._copy_btn.clicked.connect(self._copy_suggestion)
        ab.addWidget(self._copy_btn)

        self._speak_btn = self._action_btn("🔊  SPEAK", C_AMBER)
        self._speak_btn.clicked.connect(self._speak_suggestion)
        ab.addWidget(self._speak_btn)

        self._clear_btn = self._action_btn("✕  CLEAR", C_TEXT_DIM)
        self._clear_btn.clicked.connect(self._clear)
        ab.addWidget(self._clear_btn)

        layout.addWidget(action_bar)

        self.setFixedWidth(820)
        self.adjustSize()

    def _icon_btn_css(self) -> str:
        return f"""
            QPushButton {{
                background: transparent;
                color: {C_TEXT_DIM};
                border: 1px solid {C_BORDER};
                border-radius: 3px;
                font-size: 10px;
            }}
            QPushButton:hover {{ background: {C_BORDER}; color: white; }}
        """

    def _action_btn_css(self, color: str) -> str:
        # Convert hex color to rgba for hover/pressed tints
        c = QColor(color)
        r, g, b = c.red(), c.green(), c.blue()
        return f"""
            QPushButton {{
                background: transparent;
                color: {color};
                border: 1px solid {color};
                border-radius: 3px;
                padding: 0 10px;
                letter-spacing: 1px;
            }}
            QPushButton:hover {{ background: rgba({r},{g},{b},0.12); }}
            QPushButton:pressed {{ background: rgba({r},{g},{b},0.25); }}
        """

    def _action_btn(self, label: str, color: str) -> QPushButton:
        btn = QPushButton(label)
        btn.setFixedHeight(32)
        btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        btn.setStyleSheet(self._action_btn_css(color))
        return btn

    # ── Positioning ────────────────────────────────────────────────────────────

    def _position_bottom(self):
        screen = QApplication.primaryScreen().availableGeometry()
        self.adjustSize()
        x = (screen.width() - self.width()) // 2
        y = screen.height() - self.height() - 20
        self.move(x, y)

    # ── Dragging ───────────────────────────────────────────────────────────────

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag_pos and e.buttons() == Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None

    # ── Meeting detection ──────────────────────────────────────────────────────

    def _check_meeting(self):
        app = detect_meeting_app()
        if app:
            self._meet_badge.setText(f"● {app}")
            self._meet_badge.setStyleSheet(f"""
                color: {C_GREEN};
                background: rgba(0,255,136,0.08);
                padding: 2px 8px;
                border: 1px solid {C_GREEN};
                border-radius: 3px;
                font-family: 'Courier New';
                font-size: 8pt;
                font-weight: bold;
            """)
        else:
            self._meet_badge.setText("NO MEETING DETECTED")
            self._meet_badge.setStyleSheet(f"""
                color: {C_TEXT_DIM};
                background: transparent;
                padding: 2px 6px;
                border: 1px solid {C_BORDER};
                border-radius: 3px;
                font-family: 'Courier New';
                font-size: 8pt;
            """)

    # ── Scan screen ────────────────────────────────────────────────────────────

    def _scan_screen(self):
        self._scan_btn.setEnabled(False)
        self._set_status("SCANNING...")
        self._suggestion_lbl.setText("Analyzing screen...")

        # Hide overlay so it doesn't appear in its own screenshot, then restore
        self.hide()
        def _start_after_hide():
            self._scan_worker = ScreenAnalysisWorker()
            self._scan_worker.result.connect(self._on_suggestion)
            self._scan_worker.result.connect(lambda _: self.show())
            self._scan_worker.status.connect(self._set_status)
            self._scan_worker.finished.connect(lambda: self._scan_btn.setEnabled(True))
            self._scan_worker.start()
        QTimer.singleShot(150, _start_after_hide)  # 150ms for window to fully hide

    # ── Smart Listen toggle ────────────────────────────────────────────────────

    def _toggle_listen(self):
        if self._listening:
            meeting_listener.stop()
            self._listening = False
            self._listen_btn.setText("🎧  START LISTEN")
            self._listen_btn.setStyleSheet(self._action_btn_css(C_GREEN))
            self._set_status("STANDBY")
            self._status_dot.set_color(C_TEXT_DIM)
        else:
            meeting_listener.start(
                on_transcript=lambda t: self._transcript_sig.emit(t),
                on_suggestion=lambda s: self._suggestion_sig.emit(s),
            )
            self._listening = True
            self._listen_btn.setText("🔴  STOP LISTEN")
            self._listen_btn.setStyleSheet(self._action_btn_css(C_RED))
            self._set_status("LIVE")
            self._status_dot.set_color(C_GREEN)
            if self._placeholder:
                self._placeholder.hide()

    # ── Transcript ─────────────────────────────────────────────────────────────

    def _on_transcript(self, text: str):
        line = TranscriptLine(f"» {text}")
        count = self._transcript_layout.count()
        self._transcript_layout.insertWidget(count - 1, line)
        # Keep max 5 lines
        while self._transcript_layout.count() > 7:
            item = self._transcript_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()
        QTimer.singleShot(50, lambda: self._transcript_scroll.verticalScrollBar().setValue(
            self._transcript_scroll.verticalScrollBar().maximum()
        ))

    # ── Suggestion ─────────────────────────────────────────────────────────────

    def _on_suggestion(self, text: str):
        self._current_suggestion = text
        hint = meeting_listener.actionable_hint(text)
        display_text = f"{text}\n\n{hint}" if hint else text
        self._suggestion_lbl.setText(display_text)
        self._set_status("LIVE")
        # Flash border
        self._suggestion_lbl.setStyleSheet(f"""
            color: {C_TEXT};
            background: rgba(0,180,220,0.08);
            border: 1px solid {C_CYAN};
            border-radius: 3px;
            padding: 8px 10px;
        """)
        QTimer.singleShot(800, self._reset_suggestion_style)

    def _reset_suggestion_style(self):
        self._suggestion_lbl.setStyleSheet(f"""
            color: {C_TEXT};
            background: {C_PANEL};
            border: 1px solid {C_BORDER};
            border-radius: 3px;
            padding: 8px 10px;
        """)

    # ── Status ─────────────────────────────────────────────────────────────────

    def _set_status(self, text: str):
        self._status_lbl.setText(text)
        if text in ("LIVE", "LISTENING"):
            color = C_GREEN
        elif text in ("SCANNING...", "PROCESSING"):
            color = C_AMBER
        elif "ERROR" in text:
            color = C_RED
        else:
            color = C_TEXT_DIM
        self._status_lbl.setStyleSheet(f"color: {color}; background: transparent; letter-spacing: 1px; font-family: 'Courier New'; font-size: 8pt; font-weight: bold;")
        self._status_dot.set_color(color)

    # ── Action handlers ────────────────────────────────────────────────────────

    def _copy_suggestion(self):
        if self._current_suggestion:
            import terminal
            terminal.set_clipboard(self._current_suggestion)
            self._copy_btn.setText("✓  COPIED")
            QTimer.singleShot(1500, lambda: self._copy_btn.setText("📋  COPY"))

    def _speak_suggestion(self):
        if self._current_suggestion:
            threading.Thread(
                target=speak,
                args=(self._current_suggestion,),
                daemon=True
            ).start()

    def _clear(self):
        self._current_suggestion = ""
        self._suggestion_lbl.setText("Awaiting input...")
        # Clear transcript
        while self._transcript_layout.count() > 1:
            item = self._transcript_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

    # ── Thread-safe show/hide/toggle slots ────────────────────────────────────

    @pyqtSlot()
    def show_safe(self):
        self.show()
        self.raise_()

    @pyqtSlot()
    def hide_safe(self):
        self.hide()

    @pyqtSlot()
    def toggle_safe(self):
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()

    @pyqtSlot()
    def scan_safe(self):
        if not self.isVisible():
            self.show()
        self._scan_screen()

    # ── Stealth + fullscreen space support ────────────────────────────────────

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(100, self._apply_macos_window_props)

    def _apply_macos_window_props(self):
        """
        1. Hide from screen share (NSWindowSharingNone).
        2. Allow window to appear in ALL spaces, including fullscreen Zoom/Teams.
        3. Raise window level above normal windows.
        """
        stealth.apply_stealth(int(self.winId()))
        try:
            from AppKit import NSApp
            # NSWindowCollectionBehaviorCanJoinAllSpaces = 1 << 0
            # NSWindowCollectionBehaviorStationary        = 1 << 4 (don't expose in Mission Control)
            JOIN_ALL = (1 << 0) | (1 << 4)
            # NSStatusBarWindowLevel = 25
            STATUS_LEVEL = 25
            win_id = int(self.winId())
            for ns_win in NSApp.windows():
                if ns_win.windowNumber() == win_id:
                    ns_win.setCollectionBehavior_(JOIN_ALL)
                    ns_win.setLevel_(STATUS_LEVEL)
                    break
        except Exception as e:
            print(f"[Overlay] Could not set macOS window properties: {e}")


# ── Public API ─────────────────────────────────────────────────────────────────

def get_overlay() -> MeetingOverlay:
    """Return the singleton overlay, creating it if needed."""
    global _instance
    if _instance is None:
        _instance = MeetingOverlay()
    return _instance


def show():
    QMetaObject.invokeMethod(get_overlay(), "show_safe", Qt.ConnectionType.QueuedConnection)


def hide():
    if _instance:
        QMetaObject.invokeMethod(_instance, "hide_safe", Qt.ConnectionType.QueuedConnection)


def toggle():
    QMetaObject.invokeMethod(get_overlay(), "toggle_safe", Qt.ConnectionType.QueuedConnection)


def scan_screen():
    """Trigger a screen scan from outside (hotkey callback)."""
    QMetaObject.invokeMethod(get_overlay(), "scan_safe", Qt.ConnectionType.QueuedConnection)
