"""
AgentStatusPanel — shows background brain agent activity in the Jarvis HUD.

Displays: brain daemon agent last-run times, overnight training status,
token usage stats, active model. Docks to right side of JarvisWindow.
"""

from __future__ import annotations

try:
    from PyQt6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve
    from PyQt6.QtWidgets import QFrame, QVBoxLayout, QHBoxLayout, QLabel, QWidget, QPushButton
    from PyQt6.QtGui import QFont, QColor
    _PYQT_AVAILABLE = True
except ImportError:
    _PYQT_AVAILABLE = False

# Color palette — must match ui.py constants
C_BG = "#020A10"
C_BG2 = "#030D14"
C_CYAN = "#00D4FF"
C_CYAN_DIM = "#0099BB"
C_BORDER = "#0D4F70"
C_TEXT = "#A8E6FF"
C_TEXT_DIM = "#4A8FA8"
C_GREEN = "#00FF88"
C_ORANGE = "#FF6B00"
C_WARNING = "#FFAA00"
C_WHITE_DIM = "#D8F6FF"


def _glass_panel_css(border=C_BORDER, fill="rgba(2, 16, 24, 210)", radius=10):
    """Glass panel style matching ui.py."""
    return f"""
        background-color: {fill};
        border: 1px solid {border};
        border-radius: {radius}px;
    """


class AgentStatusRow(QWidget):
    """Shows: agent name, status dot (green=active, yellow=running, dim=idle), last run time."""

    def __init__(self, agent_name: str, parent=None):
        super().__init__(parent)
        self.agent_name = agent_name
        self._status_color = C_CYAN_DIM  # default idle
        self._last_run_text = "--"

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(8)

        # Status dot
        self._dot = QFrame()
        self._dot.setFixedSize(8, 8)
        self._dot.setStyleSheet(f"background-color: {C_CYAN_DIM}; border-radius: 4px;")
        layout.addWidget(self._dot, alignment=Qt.AlignmentFlag.AlignTop)

        # Text block
        text_block = QVBoxLayout()
        text_block.setContentsMargins(0, 0, 0, 0)
        text_block.setSpacing(2)

        name_lbl = QLabel(agent_name)
        name_lbl.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        name_lbl.setStyleSheet(f"color: {C_TEXT_DIM}; background: transparent; letter-spacing: 1px;")
        text_block.addWidget(name_lbl)

        self._time_lbl = QLabel(self._last_run_text)
        self._time_lbl.setFont(QFont("Courier New", 8))
        self._time_lbl.setStyleSheet(f"color: {C_WHITE_DIM}; background: transparent;")
        text_block.addWidget(self._time_lbl)

        layout.addLayout(text_block, stretch=1)
        self.setStyleSheet(_glass_panel_css(fill="rgba(3, 18, 28, 150)", radius=6))

    def set_status(self, status: str, last_run: str | None = None):
        """Update status: 'active' (green), 'running' (orange), 'idle' (cyan_dim)."""
        color_map = {
            "active": C_GREEN,
            "running": C_WARNING,
            "idle": C_CYAN_DIM,
        }
        self._status_color = color_map.get(status, C_CYAN_DIM)
        self._dot.setStyleSheet(f"background-color: {self._status_color}; border-radius: 4px;")
        if last_run:
            self._time_lbl.setText(last_run)


class TrainingProgressBar(QWidget):
    """Shows overnight training status: 'TRAINING 11pm-7am' or 'IDLE — next: 11:00pm'."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        header = QLabel("TRAINING PACK")
        header.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        header.setStyleSheet(f"color: {C_CYAN}; background: transparent; letter-spacing: 1px;")
        layout.addWidget(header)

        self._status_lbl = QLabel("IDLE — next: 11:00pm")
        self._status_lbl.setFont(QFont("Courier New", 8))
        self._status_lbl.setStyleSheet(f"color: {C_WHITE_DIM}; background: transparent;")
        layout.addWidget(self._status_lbl)

        self.setStyleSheet(_glass_panel_css(fill="rgba(3, 18, 28, 150)", radius=6))

    def set_training_status(self, active: bool, next_run: str = ""):
        """Set training status."""
        if active:
            self._status_lbl.setText("TRAINING 11pm-7am")
            self._status_lbl.setStyleSheet(f"color: {C_GREEN}; background: transparent;")
        else:
            text = f"IDLE — next: {next_run}" if next_run else "IDLE"
            self._status_lbl.setText(text)
            self._status_lbl.setStyleSheet(f"color: {C_TEXT_DIM}; background: transparent;")


class AgentStatusPanel(QFrame):
    """Collapsible side panel showing background agent activity."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("AgentStatusPanel")
        self.setMinimumWidth(220)
        self.setMaximumWidth(220)
        self.setStyleSheet(_glass_panel_css(fill="rgba(3, 18, 28, 205)", radius=12))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # Header with toggle
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(8)

        title_lbl = QLabel("BRAIN AGENTS")
        title_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        title_lbl.setStyleSheet(f"color: {C_CYAN}; background: transparent; letter-spacing: 2px;")
        header_row.addWidget(title_lbl)
        header_row.addStretch()

        self._toggle_btn = QPushButton("−")
        self._toggle_btn.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        self._toggle_btn.setFixedSize(24, 24)
        self._toggle_btn.setStyleSheet(f"""
            background-color: transparent;
            color: {C_CYAN};
            border: 1px solid {C_BORDER};
            border-radius: 4px;
            font-weight: bold;
        """)
        self._toggle_btn.clicked.connect(self._toggle_collapsed)
        header_row.addWidget(self._toggle_btn)
        layout.addLayout(header_row)

        # Agent rows
        self._agent_rows = {}
        agents = ["Memory Agent", "Vault Sync", "Knowledge Feed", "Eval Agent"]
        for agent_name in agents:
            row = AgentStatusRow(agent_name)
            self._agent_rows[agent_name] = row
            layout.addWidget(row)

        # Training status
        self._training_widget = TrainingProgressBar()
        layout.addWidget(self._training_widget)

        # Token section
        token_label = QLabel("TOKEN USAGE")
        token_label.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        token_label.setStyleSheet(f"color: {C_CYAN}; background: transparent; letter-spacing: 1px;")
        layout.addWidget(token_label)

        self._token_info = QLabel("0 calls · $0.00")
        self._token_info.setFont(QFont("Courier New", 8))
        self._token_info.setStyleSheet(f"color: {C_TEXT}; background: transparent;")
        layout.addWidget(self._token_info)

        # Model section
        model_label = QLabel("ACTIVE MODEL")
        model_label.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        model_label.setStyleSheet(f"color: {C_CYAN}; background: transparent; letter-spacing: 1px;")
        layout.addWidget(model_label)

        self._model_name = QLabel("claude-sonnet")
        self._model_name.setFont(QFont("Courier New", 8))
        self._model_name.setStyleSheet(f"color: {C_WHITE_DIM}; background: transparent;")
        layout.addWidget(self._model_name)

        layout.addStretch()

        self._collapsed = False
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._on_refresh)
        self._refresh_timer.start(30000)  # 30 seconds

    def _toggle_collapsed(self):
        """Toggle panel collapsed state."""
        self._collapsed = not self._collapsed
        self._toggle_btn.setText("+" if self._collapsed else "−")
        # Simple show/hide for content rows
        for row in self._agent_rows.values():
            row.setVisible(not self._collapsed)
        self._training_widget.setVisible(not self._collapsed)
        self._token_info.setVisible(not self._collapsed)
        self._model_name.setVisible(not self._collapsed)

    def _on_refresh(self):
        """Called by timer to update display."""
        self.update_from_daemon({})

    def update_from_daemon(self, daemon_status: dict):
        """Update all rows from brain_daemon.status() dict."""
        # Update agent rows
        for agent_name, row in self._agent_rows.items():
            agent_key = agent_name.lower().replace(" ", "_")
            agent_info = daemon_status.get(agent_key, {})
            status = agent_info.get("status", "idle")
            last_run = agent_info.get("last_run", None)
            row.set_status(status, last_run)

        # Update training status
        training_active = daemon_status.get("training", {}).get("active", False)
        next_run = daemon_status.get("training", {}).get("next_run", "11:00pm")
        self._training_widget.set_training_status(training_active, next_run)

        # Update token info
        try:
            import usage_tracker
            summary = usage_tracker.summarize(hours=24)
            call_count = summary.get("call_count", 0)
            cost = summary.get("estimated_cost_usd", 0.0)
            self._token_info.setText(f"{call_count} calls · ${cost:.4f}")
        except Exception:
            self._token_info.setText("-- calls · --")

        # Update model name
        model = daemon_status.get("model", "claude-sonnet")
        self._model_name.setText(model)
