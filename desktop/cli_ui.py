"""
CLI UI for Jarvis headless mode — Rich-based terminal interface.

Matches the visual style of GitHub Copilot CLI and Claude Code:
- Status bar at bottom
- Colored message output
- Streaming with live updates
- Model/token indicator
"""

import sys
from typing import Generator, Optional

# Try to import rich, fall back to plain ANSI
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.text import Text
    _RICH_AVAILABLE = True
except ImportError:
    _RICH_AVAILABLE = False


class CLISession:
    """Manage a full headless session with history and status bar updates."""

    def __init__(self, use_rich: bool = True):
        self.use_rich = use_rich and _RICH_AVAILABLE
        if self.use_rich:
            self.console = Console()
        self.history = []
        self.current_model = "claude-sonnet"
        self.current_tokens = 0
        self.current_cost = 0.0
        self.agents_active = 0

    def print_header(self):
        """Print Jarvis header."""
        if self.use_rich:
            header_text = Text()
            header_text.append("J.A.R.V.I.S", style="bold cyan")
            header_text.append("  v2.0  ", style="cyan")
            header_text.append("⬡ LOCAL", style="bold green")
            panel = Panel(
                header_text,
                border_style="cyan",
                padding=(1, 2),
            )
            self.console.print(panel)
            model_line = Text()
            model_line.append("Model: ", style="dim cyan")
            model_line.append(self.current_model, style="cyan")
            self.console.print(model_line)
            self.console.print()
        else:
            print("\n╔══════════════════════════════════╗")
            print("║  J.A.R.V.I.S  v2.0  ⬡ LOCAL    ║")
            print("║  Model: " + self.current_model.ljust(24) + "║")
            print("╚══════════════════════════════════╝\n")

    def print_user_message(self, text: str):
        """Print user message — right-aligned with 'YOU ▶' prefix in orange."""
        self.history.append(("user", text))
        if self.use_rich:
            msg = Text()
            msg.append("YOU ▶ ", style="bold orange1")
            msg.append(text, style="dim white")
            self.console.print(msg, highlight=False)
        else:
            print(f"\n[33mYOU ▶[0m {text}")
        print()

    def print_jarvis_message(self, text: str, model: str = ""):
        """Print Jarvis message — left-aligned 'JARVIS ▶' in cyan."""
        self.history.append(("jarvis", text, model or self.current_model))
        if self.use_rich:
            msg = Text()
            msg.append("JARVIS ▶ ", style="bold cyan")
            msg.append(text, style="white")
            if model:
                msg.append(f"  [{model}]", style="dim cyan")
            self.console.print(msg, highlight=False)
        else:
            model_tag = f"  [{model}]" if model else ""
            print(f"\n[36mJARVIS ▶[0m {text}{model_tag}")
        print()

    def print_thinking_indicator(self) -> "ThinkingIndicator":
        """Return a context manager that prints animated thinking dots."""
        return ThinkingIndicator(self)

    def print_status_bar(
        self,
        model: str = "",
        tokens: int = 0,
        cost_usd: float = 0.0,
        agents_active: int = 0,
    ):
        """Print bottom status line: [model] tokens · $cost · agents active."""
        self.current_model = model or self.current_model
        self.current_tokens = tokens
        self.current_cost = cost_usd
        self.agents_active = agents_active

        status_parts = []
        if model:
            status_parts.append(f"[{model}]")
        if tokens > 0:
            status_parts.append(f"{tokens:,} tokens")
        if cost_usd > 0:
            status_parts.append(f"${cost_usd:.6f}")
        if agents_active > 0:
            status_parts.append(f"{agents_active} agents active")

        status_line = " · ".join(status_parts) if status_parts else "—"

        if self.use_rich:
            self.console.print(f"\n[dim cyan]{status_line}[/dim cyan]")
        else:
            print(f"\n[36m{status_line}[0m")

    def stream_jarvis_response(self, generator: Generator[str, None, None]) -> str:
        """Consume generator, print tokens as they arrive, return full text.

        Shows a thinking indicator until the first token arrives so the terminal
        never goes silent during LLM generation (the ThinkingIndicator in main.py
        only covers the routing phase; this covers the generation phase).
        """
        full_text = ""
        first_token = True

        # Thinking indicator — visible until the first token replaces it.
        if self.use_rich:
            self.console.print(
                "JARVIS [dim cyan]···[/dim cyan]",
                end="",
                style="bold cyan",
                highlight=False,
            )
        else:
            print("\033[36mJARVIS\033[0m \033[36m···\033[0m", end="", flush=True)

        for chunk in generator:
            if first_token:
                first_token = False
                # Swap indicator for the response header on first real token.
                if self.use_rich:
                    self.console.print()  # end indicator line
                    self.console.print("JARVIS ▶ ", style="bold cyan", end="", highlight=False)
                else:
                    sys.stdout.write("\r" + " " * 60 + "\r")
                    print("\n\033[36mJARVIS ▶\033[0m ", end="", flush=True)
            full_text += chunk
            if self.use_rich:
                self.console.print(chunk, style="white", end="", highlight=False)
            else:
                sys.stdout.write(chunk)
                sys.stdout.flush()

        if first_token:
            # Empty generator — clear indicator quietly.
            if self.use_rich:
                self.console.print()
            else:
                sys.stdout.write("\r" + " " * 60 + "\r")
                sys.stdout.flush()

        if self.use_rich:
            self.console.print()
        else:
            print()

        self.history.append(("jarvis", full_text, self.current_model))
        print()
        return full_text

    def print_tool_call(self, tool: str, args: str):
        """Print styled tool call display."""
        if self.use_rich:
            msg = Text()
            msg.append("TOOL ", style="bold yellow")
            msg.append(f"{tool}", style="yellow")
            msg.append(" → ", style="dim")
            msg.append(args, style="dim white")
            self.console.print(msg, highlight=False)
        else:
            print(f"[33mTOOL[0m [33m{tool}[0m → {args}")
        print()


class ThinkingIndicator:
    """Context manager for animated thinking dots."""

    def __init__(self, session: CLISession):
        self.session = session
        self.active = False

    def __enter__(self):
        self.active = True
        if self.session.use_rich:
            self.session.console.print(
                "JARVIS [dim cyan]···[/dim cyan]",
                end="",
                style="bold cyan",
                highlight=False,
            )
        else:
            print("[36mJARVIS[0m [36m···[0m", end="", flush=True)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.active = False
        # Clear the line
        sys.stdout.write("\r" + " " * 50 + "\r")
        sys.stdout.flush()


def print_jarvis_header(use_rich: bool = True):
    """Print Jarvis ASCII header."""
    if use_rich and _RICH_AVAILABLE:
        console = Console()
        header_text = Text()
        header_text.append("J.A.R.V.I.S", style="bold cyan")
        header_text.append("  v2.0  ", style="cyan")
        header_text.append("⬡ LOCAL", style="bold green")
        panel = Panel(
            header_text,
            border_style="cyan",
            padding=(1, 2),
        )
        console.print(panel)
    else:
        print("\n╔══════════════════════════════════╗")
        print("║  J.A.R.V.I.S  v2.0  ⬡ LOCAL    ║")
        print("╚══════════════════════════════════╝\n")


def print_user_message(text: str, use_rich: bool = True):
    """Print user message with 'YOU ▶' prefix in orange."""
    if use_rich and _RICH_AVAILABLE:
        console = Console()
        msg = Text()
        msg.append("YOU ▶ ", style="bold orange1")
        msg.append(text, style="dim white")
        console.print(msg, highlight=False)
    else:
        print(f"[33mYOU ▶[0m {text}")


def print_jarvis_message(text: str, model: str = "", use_rich: bool = True):
    """Print Jarvis message with 'JARVIS ▶' prefix in cyan."""
    if use_rich and _RICH_AVAILABLE:
        console = Console()
        msg = Text()
        msg.append("JARVIS ▶ ", style="bold cyan")
        msg.append(text, style="white")
        if model:
            msg.append(f"  [{model}]", style="dim cyan")
        console.print(msg, highlight=False)
    else:
        model_tag = f"  [{model}]" if model else ""
        print(f"[36mJARVIS ▶[0m {text}{model_tag}")


def print_thinking_indicator(use_rich: bool = True):
    """Return context manager for animated thinking indicator."""
    return ThinkingIndicator(CLISession(use_rich=use_rich))


def print_status_bar(
    model: str = "",
    tokens: int = 0,
    cost_usd: float = 0.0,
    agents_active: int = 0,
    use_rich: bool = True,
):
    """Print status bar at bottom."""
    status_parts = []
    if model:
        status_parts.append(f"[{model}]")
    if tokens > 0:
        status_parts.append(f"{tokens:,} tokens")
    if cost_usd > 0:
        status_parts.append(f"${cost_usd:.6f}")
    if agents_active > 0:
        status_parts.append(f"{agents_active} agents active")

    status_line = " · ".join(status_parts) if status_parts else "—"

    if use_rich and _RICH_AVAILABLE:
        console = Console()
        console.print(f"\n[dim cyan]{status_line}[/dim cyan]")
    else:
        print(f"\n[36m{status_line}[0m")


def stream_jarvis_response(
    generator: Generator[str, None, None],
    use_rich: bool = True,
) -> str:
    """Consume generator, stream output, return full text."""
    full_text = ""

    if use_rich and _RICH_AVAILABLE:
        console = Console()
        console.print("JARVIS ▶ ", style="bold cyan", end="", highlight=False)
        for chunk in generator:
            full_text += chunk
            console.print(chunk, style="white", end="", highlight=False)
        console.print()
    else:
        print("[36mJARVIS ▶[0m ", end="", flush=True)
        for chunk in generator:
            full_text += chunk
            sys.stdout.write(chunk)
            sys.stdout.flush()
        print()

    return full_text


def print_tool_call(tool: str, args: str, use_rich: bool = True):
    """Print tool call in styled format."""
    if use_rich and _RICH_AVAILABLE:
        console = Console()
        msg = Text()
        msg.append("TOOL ", style="bold yellow")
        msg.append(f"{tool}", style="yellow")
        msg.append(" → ", style="dim")
        msg.append(args, style="dim white")
        console.print(msg, highlight=False)
    else:
        print(f"[33mTOOL[0m [33m{tool}[0m → {args}")
