"""Standalone macOS system tray status panel for Jarvis."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Match main.py's conda-safe Qt discovery before importing PyQt6.
_pyqt6_plugins = Path(sys.executable).parent.parent / "lib" / (
    f"python{sys.version_info.major}.{sys.version_info.minor}"
) / "site-packages" / "PyQt6" / "Qt6" / "plugins"
if _pyqt6_plugins.is_dir():
    os.environ.setdefault("QT_PLUGIN_PATH", str(_pyqt6_plugins))
    os.environ.setdefault(
        "QT_QPA_PLATFORM_PLUGIN_PATH",
        str(_pyqt6_plugins / "platforms"),
    )

from PyQt6.QtCore import QProcess, QTimer, Qt
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon


REPO_ROOT = Path(__file__).resolve().parents[1]
STATUS_PATH = REPO_ROOT / "ORCHESTRATOR_STATUS.json"
JARVIS_APP = Path.home() / "Applications" / "Jarvis.app"
REFRESH_MS = 5_000

STATE_COLORS = {
    "idle": QColor("#22c55e"),
    "running": QColor("#eab308"),
    "error": QColor("#ef4444"),
}


@dataclass(frozen=True)
class StatusSnapshot:
    state: str
    task: str
    detail: str


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def snapshot_from_payload(payload: dict[str, Any]) -> StatusSnapshot:
    sessions = payload.get("sessions")
    if not isinstance(sessions, list):
        return StatusSnapshot("error", "Status unavailable", "Missing sessions list")

    valid_sessions = [item for item in sessions if isinstance(item, dict)]
    states = {str(item.get("status", "")).strip().lower() for item in valid_sessions}
    if states & {"error", "failed", "failure"}:
        state = "error"
    elif states & {"active", "running", "in_progress"}:
        state = "running"
    else:
        state = "idle"

    latest = max(
        valid_sessions,
        key=lambda item: _parse_timestamp(item.get("last_active")),
        default={},
    )
    task = str(latest.get("current_task") or "No task recorded").strip()
    session_name = str(latest.get("name") or "Jarvis").strip()
    detail = f"{session_name}: {task}"
    return StatusSnapshot(state, task, detail)


def load_snapshot(path: Path = STATUS_PATH) -> StatusSnapshot:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("status root must be an object")
        return snapshot_from_payload(payload)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return StatusSnapshot("error", "Status unavailable", str(exc))


def status_icon(state: str) -> QIcon:
    pixmap = QPixmap(22, 22)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QColor("#ffffff"))
    painter.setBrush(STATE_COLORS.get(state, STATE_COLORS["error"]))
    painter.drawEllipse(3, 3, 16, 16)
    painter.end()
    return QIcon(pixmap)


class JarvisTray(QSystemTrayIcon):
    def __init__(self, app: QApplication) -> None:
        super().__init__()
        self._app = app
        self._snapshot = StatusSnapshot("error", "Status unavailable", "Not loaded")

        menu = QMenu()
        open_action = QAction("Open Jarvis", menu)
        open_action.triggered.connect(self._open_jarvis)
        menu.addAction(open_action)

        status_action = QAction("Last task status", menu)
        status_action.triggered.connect(self._show_last_task)
        menu.addAction(status_action)

        menu.addSeparator()
        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(app.quit)
        menu.addAction(quit_action)
        self.setContextMenu(menu)

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def refresh(self) -> None:
        self._snapshot = load_snapshot()
        self.setIcon(status_icon(self._snapshot.state))
        self.setToolTip(f"Jarvis: {self._snapshot.state}")

    def _open_jarvis(self) -> None:
        started, _ = QProcess.startDetached("/usr/bin/open", [str(JARVIS_APP)])
        if not started:
            self.showMessage(
                "Jarvis",
                f"Could not open {JARVIS_APP}",
                QSystemTrayIcon.MessageIcon.Critical,
            )

    def _show_last_task(self) -> None:
        self.showMessage(
            f"Jarvis: {self._snapshot.state}",
            self._snapshot.detail,
            QSystemTrayIcon.MessageIcon.Information,
        )


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Jarvis Tray")
    app.setQuitOnLastWindowClosed(False)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        return 1

    tray = JarvisTray(app)
    tray.show()
    return app.exec()  # Qt event loop method, not Python exec.  # pre-commit-ok


if __name__ == "__main__":
    raise SystemExit(main())
