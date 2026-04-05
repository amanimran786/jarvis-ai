"""
Jarvis Orchestrator — replaces regex routing with LLM intent classification.

Instead of 200+ lines of keyword matching, Haiku reads the user's intent
and selects the right tool in ~300ms. Falls back to smart_stream on failure.

Architecture:
  user input → orchestrator.classify() → ToolDecision
                                              ↓
                              router dispatches the right tool/agent

Tool registry is the single source of truth — add new tools here and the
orchestrator automatically knows about them.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any

from brain_claude import ask_claude
from config import HAIKU

# ── Tool registry ─────────────────────────────────────────────────────────────

TOOLS = {
    "chat": "General conversation, questions, explanations, advice, opinions.",

    "search": "Web search for current news, facts, prices, recent events. "
              "Triggers: 'search for', 'look up', 'google', 'find information', 'what is X'.",

    "deep_research": "Multi-step research producing a cited report. Use when the user "
                     "wants thorough, sourced research on a topic — not a quick lookup. "
                     "Triggers: 'research', 'deep dive', 'full report', 'write a report on'.",

    "operative": "Autonomous multi-step task execution. Use when the user describes a "
                 "chained workflow involving multiple actions. "
                 "Example: 'research X then write a report and email it to me', "
                 "'find the best Python libraries for X, save a summary, and open VS Code'.",

    "calendar": "Google Calendar — read events, create events, check schedule. "
                "Triggers: 'my schedule', 'calendar', 'what do I have today', 'create event'.",

    "email": "Gmail — read unread emails, check inbox. "
             "Triggers: 'check email', 'my inbox', 'unread emails', 'any emails'.",

    "weather": "Current weather. Triggers: 'weather', 'temperature', 'forecast'.",

    "timer": "Set a countdown timer or reminder. "
             "Triggers: 'timer', 'remind me in', 'set a timer for'.",

    "system": "macOS system control — volume, brightness, screenshot, lock screen, mute. "
              "Triggers: 'volume', 'brightness', 'screenshot', 'mute', 'lock'.",

    "app": "Launch a macOS application. "
           "Triggers: 'open', 'launch', 'start' followed by app name.",

    "terminal": "Run a shell command, read/write files, list directory. "
                "Triggers: 'run', 'execute', 'terminal', 'command', 'read file', 'list files'.",

    "notes": "Take, read, search personal notes. "
             "Triggers: 'note', 'write this down', 'my notes', 'find a note'.",

    "camera": "Webcam capture or screen analysis. "
              "Triggers: 'what do you see', 'look at this', 'what's on my screen', 'read this'.",

    "memory": "Save or recall personal facts. "
              "Triggers: 'remember', 'forget', 'what do you know about me', 'briefing'.",

    "hardware": "Control physical hardware devices via serial/USB or WiFi. "
                "Triggers: device names, 'fire', 'activate', 'trigger', 'relay', 'hardware status'.",

    "self_improve": "Modify Jarvis's own source code. "
                    "Triggers: 'improve yourself', 'modify your', 'update your code', "
                    "'upgrade your', 'change your interface'.",

    "meeting": "Smart Listen — tap call audio and get real-time suggestions. "
               "Triggers: 'smart listen', 'listen to call', 'meeting mode', 'setup blackhole'.",
}

_TOOL_LIST = "\n".join(f'  "{k}": {v}' for k, v in TOOLS.items())

_SYSTEM = f"""You are Jarvis's intent classifier. Given user input, select the best tool.

Available tools:
{_TOOL_LIST}

Rules:
- Return ONLY valid JSON, nothing else.
- Choose the single most specific tool.
- If the request chains multiple distinct actions (research + save + email), use "operative".
- "chat" is the fallback for anything conversational or unclear.
- Extract any relevant params from the input.

Response format:
{{"tool": "<tool_name>", "confidence": 0.0-1.0, "action": "<specific action>", "params": {{}}}}

Examples:
  "set a timer for 5 minutes"   → {{"tool":"timer","confidence":0.99,"action":"set","params":{{"seconds":300,"label":"5 minutes"}}}}
  "what's the weather like"     → {{"tool":"weather","confidence":0.98,"action":"get","params":{{}}}}
  "research quantum computing and write me a report" → {{"tool":"deep_research","confidence":0.97,"action":"research","params":{{"query":"quantum computing"}}}}
  "research AI trends then save the report and email it to me" → {{"tool":"operative","confidence":0.96,"action":"run","params":{{"task":"research AI trends then save the report and email it to me"}}}}
  "how are you doing today"     → {{"tool":"chat","confidence":0.99,"action":"converse","params":{{}}}}
"""


# ── Decision dataclass ────────────────────────────────────────────────────────

@dataclass
class ToolDecision:
    tool:       str
    confidence: float
    action:     str
    params:     dict = field(default_factory=dict)
    raw:        str  = ""

    @property
    def high_confidence(self) -> bool:
        return self.confidence >= 0.75


# ── Classification ────────────────────────────────────────────────────────────

_FALLBACK = ToolDecision(tool="chat", confidence=0.5, action="converse")


def classify(user_input: str) -> ToolDecision:
    """
    Classify user intent. Returns ToolDecision.
    Fast path: skip orchestration for unambiguous short commands.
    Full path: Haiku classification (~300ms).
    """
    # Fast-path: never send pure gibberish or wake-word echoes to the LLM
    if len(user_input.strip()) < 3:
        return _FALLBACK

    # Fast-path: some patterns are so unambiguous it's faster to skip the LLM
    fast = _fast_classify(user_input.lower().strip())
    if fast:
        return fast

    # Full LLM classification
    try:
        raw = ask_claude(
            user_input,
            model=HAIKU,
            system=_SYSTEM,
        )
        return _parse(raw)
    except Exception as e:
        print(f"[Orchestrator] Classification failed: {e}")
        return _FALLBACK


def _fast_classify(lower: str) -> ToolDecision | None:
    """Instant classification for high-frequency, unambiguous intents."""
    # Timer — very specific
    if re.search(r"\b(timer|remind me in|set a timer)\b", lower):
        return ToolDecision("timer", 0.99, "set")
    # Volume/brightness/system
    if re.search(r"\b(volume|mute|unmute|brightness|screenshot|lock screen|lock my screen)\b", lower):
        return ToolDecision("system", 0.99, "control")
    # App open
    if re.match(r"\b(open|launch|start)\b\s+\w+", lower) and "interface" not in lower:
        app = re.sub(r"^(open|launch|start)\s+", "", lower).strip()
        return ToolDecision("app", 0.99, "open", {"app": app})
    # Weather
    if re.search(r"\b(weather|forecast|temperature)\b", lower):
        return ToolDecision("weather", 0.99, "get")
    # Email
    if re.search(r"\b(check (my )?email|unread emails|my inbox|any emails)\b", lower):
        return ToolDecision("email", 0.99, "read")
    # Calendar
    if re.search(r"\b(my schedule|my calendar|what do i have today|any events)\b", lower):
        return ToolDecision("calendar", 0.99, "read")
    # Memory
    if re.match(r"^(remember |forget )", lower):
        return ToolDecision("memory", 0.99, "save" if lower.startswith("remember") else "forget")
    # Self-improve
    if re.search(r"\b(improve yourself|modify your|upgrade your|change your interface|redesign your)\b", lower):
        return ToolDecision("self_improve", 0.99, "improve")
    # Restart
    if re.search(r"\b(restart yourself|restart jarvis|reload yourself|apply changes)\b", lower):
        return ToolDecision("self_improve", 0.99, "restart")
    return None


def _parse(raw: str) -> ToolDecision:
    """Parse LLM JSON response into ToolDecision."""
    # Strip markdown fences
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    # Extract JSON object
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return _FALLBACK

    data = json.loads(match.group())
    tool = data.get("tool", "chat")

    # Validate tool exists
    if tool not in TOOLS:
        tool = "chat"

    return ToolDecision(
        tool=tool,
        confidence=float(data.get("confidence", 0.5)),
        action=str(data.get("action", "")),
        params=dict(data.get("params", {})),
        raw=raw,
    )
