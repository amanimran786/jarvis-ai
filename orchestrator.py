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

from brains.brain_claude import ask_claude
from config import HAIKU
import skills
import model_router
import tool_registry

# ── Tool registry ─────────────────────────────────────────────────────────────
TOOLS = tool_registry.tools()
_TOOL_LIST = tool_registry.tool_list_text()

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


def _build_system(user_input: str) -> str:
    skill_block = skills.metadata_block(user_input, limit=6)
    if not skill_block:
        return _SYSTEM
    return (
        _SYSTEM
        + "\nRelevant local skill metadata for this request:\n"
        + skill_block
        + "\nIf one of these skills is relevant, include it as params.skill_id."
    )


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
        return _attach_skill(user_input, fast)

    # In open-source mode, skip specialized agents for short queries.
    # Specialized agents run 3-5 model calls sequentially — acceptable for cloud
    # (fast), but with R1:14b locally that's 5+ minutes for a simple question.
    # Only route to specialized agents in open-source mode for genuinely complex
    # queries (20+ words) where the multi-role output quality justifies the wait.
    word_count = len(user_input.split())
    _no_cloud = model_router.is_open_source_mode() or model_router.get_mode() == "local"
    if _no_cloud and word_count < 20:
        local_short = _local_short_query_classify(user_input.lower().strip())
        if local_short:
            return _attach_skill(user_input, local_short)
        return _attach_skill(user_input, _FALLBACK)

    # Use the fast heuristic specialist classifier in every mode.
    # This is local and deterministic, unlike the cloud classifier below.
    auto_specialized = _auto_specialized_classify(user_input.lower().strip())
    if auto_specialized:
        return _attach_skill(user_input, auto_specialized)

    # In local or open-source mode skip cloud classification entirely.
    if model_router.is_open_source_mode() or model_router.get_mode() == "local":
        return _attach_skill(user_input, _FALLBACK)

    # Full LLM classification
    try:
        raw = ask_claude(
            user_input,
            model=HAIKU,
            system=_build_system(user_input),
        )
        return _attach_skill(user_input, _parse(raw))
    except Exception as e:
        print(f"[Orchestrator] Classification failed: {e}")
        return _FALLBACK


def _attach_skill(user_input: str, decision: ToolDecision) -> ToolDecision:
    if decision.params.get("skill_id") and skills.get_skill(decision.params.get("skill_id")):
        return decision
    skill = skills.choose_skill(user_input, tool=decision.tool)
    if skill:
        decision.params["skill_id"] = skill.id
    return decision


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
    # Browser
    if re.search(r"\b(browse to|open website|open site|go to https?://|go to www\.|search the web|search google for|summarize this page|reload page|go back|go forward|click (the )?(link|button))\b", lower):
        return ToolDecision("browser", 0.97, "browse")
    # Weather
    if re.search(r"\b(weather|forecast|temperature)\b", lower):
        return ToolDecision("weather", 0.99, "get")
    # Email
    if re.search(r"\b(check (my )?email|unread emails|my inbox|any emails)\b", lower):
        return ToolDecision("email", 0.99, "read")
    # Skill factory
    if re.search(r"\b(create|generate|make|build|promote)\b.*\bskill\b", lower):
        action = "create"
        if re.search(r"\b(promote|failure|failures|eval)\b", lower):
            action = "promote"
        return ToolDecision("skill", 0.96, action)
    # Local model training / distillation
    if re.search(r"\b(train|tune|improve|distill|export|build|fine tune|prepare|evaluate|eval|promote|status|check|automate|autopilot|cycle)\b.*\b(local model|local models|local eval|local evals|ollama|training data|training dataset|modelfile|distillation pipeline|training pack|handoff|axolotl|unsloth|lora|adapter)\b", lower):
        action = "status"
        if re.search(r"\bdistill\b", lower):
            action = "distill"
        elif re.search(r"\bexport\b", lower):
            action = "export"
        elif re.search(r"\bbuild\b.*\bmodelfile\b", lower):
            action = "modelfile"
        elif re.search(r"\bautomate|autopilot|cycle\b", lower):
            action = "automate"
        elif re.search(r"\bpromote\b", lower):
            action = "promote"
        elif re.search(r"\bevaluate|eval\b", lower):
            action = "evaluate"
        elif re.search(r"\bhandoff|axolotl|unsloth|lora\b", lower):
            action = "handoff"
        elif re.search(r"\btrain|tune|improve|fine tune\b", lower):
            action = "train"
        return ToolDecision("local_model", 0.96, action)
    # Local knowledge vault
    if re.search(r"\b(vault|knowledge base|local knowledge|search the vault|refresh the vault|index the vault|build the vault wiki|compile the wiki|ingest (?:source|file|repo|repository|url|notes)|wiki)\b", lower):
        action = "search"
        if re.search(r"\bingest\b", lower):
            action = "ingest"
        elif re.search(r"\b(build|compile)\b", lower):
            action = "build"
        elif re.search(r"\b(refresh|reindex|index)\b", lower):
            action = "refresh"
        elif re.search(r"\b(status|what's in|what is in|show)\b", lower):
            action = "status"
        return ToolDecision("knowledge", 0.97, action)
    # Calendar
    if re.search(r"\b(my schedule|my calendar|what do i have today|any events)\b", lower):
        return ToolDecision("calendar", 0.99, "read")
    # Memory
    if re.match(r"^(remember |forget )", lower):
        return ToolDecision("memory", 0.99, "save" if lower.startswith("remember") else "forget")
    # Admin
    if re.search(r"\b(as admin|with admin privileges|administrator privileges|run as root|sudo)\b", lower):
        return ToolDecision("admin", 0.99, "run")
    # Self-improve — require self-referential context to avoid false positives
    if re.search(r"\b(review your own code|review your code|self review|what are your shortcomings|what are your weaknesses|review yourself)\b", lower):
        return ToolDecision("self_improve", 0.99, "review")
    if re.search(r"\b(use specialized agents|use agents|multi-pass|planner executor reviewer|science expert|security reviewer|self-improve critic)\b", lower):
        return ToolDecision("specialized_agent", 0.97, "run")
    if re.search(r"\b(improve yourself|modify your (code|source|interface|routing|memory|voice)|upgrade your (code|source|interface|routing|memory|voice)|change your interface|redesign your (interface|ui|layout))\b", lower):
        return ToolDecision("self_improve", 0.99, "improve")
    # Restart
    if re.search(r"\b(restart yourself|restart jarvis|reload yourself|apply changes|hit your restart|do a restart)\b", lower):
        return ToolDecision("self_improve", 0.99, "restart")
    # Messaging — require "to <name>" or an explicit send/message verb at the START
    # to avoid false positives on phrases like "plain text in a database"
    if re.search(r"\b(text|message|send a text|send a message|imessage)\b.*(to\s+\w+|\w+\s+imran)", lower):
        # Exclude knowledge/technical sentences containing "text" as a noun
        if not re.search(r"\b(plain text|cipher text|ciphertext|clear text|in a database|html|xml|json|csv|stored|encrypt|hash)\b", lower):
            return ToolDecision("message", 0.95, "send")
    if re.search(r"^(text|send a text to|send a message to|message)\s+\w+", lower):
        return ToolDecision("message", 0.95, "send")
    return None


def _local_short_query_classify(lower: str) -> ToolDecision | None:
    """Keep common short voice queries tool-aware even without cloud classification."""
    if re.search(r"\b(next event|next meeting|what(?:'s| is) next|what do i have (today|tomorrow)|what(?:'s| is) on (my )?(calendar|schedule)|calendar (today|tomorrow)|schedule (today|tomorrow)|meetings? (today|tomorrow)|events? (today|tomorrow)|any meetings?)\b", lower):
        return ToolDecision("calendar", 0.91, "read")
    if re.search(r"\b(check (my )?inbox|check email|new emails?|unread emails?|any emails?|gmail)\b", lower):
        return ToolDecision("email", 0.9, "read")
    if re.search(r"\b(open notes|show notes|my notes|search notes|take a note|write a note|save a note|note this)\b", lower):
        action = "write" if any(term in lower for term in ("take a note", "write a note", "save a note", "note this")) else "read"
        return ToolDecision("notes", 0.9, action)
    if re.search(r"\b(meeting mode|smart listen|meeting status|start listening|stop listening)\b", lower):
        return ToolDecision("meeting", 0.9, "manage")
    if re.search(r"\b(search for|look up|google|find on the web|browse to|open this page|open that page)\b", lower):
        return ToolDecision("browser", 0.88, "browse")
    if re.search(r"\b(what do you remember|what do you know about me|briefing|catch me up|what did i miss)\b", lower):
        return ToolDecision("memory", 0.86, "recall")
    return None


def _auto_specialized_classify(lower: str) -> ToolDecision | None:
    """
    Promote clearly high-risk or high-complexity requests into a scoped
    specialist-agent pass without requiring the user to ask explicitly.
    Keep this conservative so normal requests stay cheap and direct.
    """
    word_count = len(re.findall(r"\b\w+\b", lower))
    asks_for_reasoning = bool(re.search(r"\b(why|how|explain|walk me through|tradeoff|trade-offs|compare|root cause|debug|diagnose|design|architecture|review)\b", lower))
    has_question = "?" in lower or asks_for_reasoning

    science_markers = (
        "transformer", "kv cache", "entropy", "thermodynamics", "information theory",
        "crispr", "genome", "biology", "physics", "chemistry", "semiconductor",
        "lithography", "materials science", "scientific", "science",
    )
    security_markers = (
        "security", "secure", "auth", "authentication", "authorization", "permission",
        "exploit", "vulnerability", "xss", "csrf", "sql injection", "secret", "token leak",
        "credential", "threat model", "attack surface", "encryption",
    )
    technical_markers = (
        "api", "fastapi", "nginx", "docker", "kubernetes", "postgres", "redis", "sql",
        "python", "react", "next.js", "nextjs", "thread", "concurrency", "latency",
        "throughput", "queue", "cache", "memory leak", "deadlock", "race condition",
        "distributed system", "microservice", "schema", "index", "inference",
    )
    planning_markers = (
        "plan", "approach", "sequence", "break this down", "step by step", "what should we do",
        "implementation plan", "migration plan", "rollout plan",
    )

    if any(marker in lower for marker in ("review your own code", "review your code", "self review", "what are your shortcomings", "what are your weaknesses")):
        return ToolDecision("specialized_agent", 0.95, "run", {"roles": ["self_improve_critic", "reviewer"]})

    if any(marker in lower for marker in security_markers) and (has_question or word_count >= 8):
        return ToolDecision("specialized_agent", 0.9, "run", {"roles": ["security_reviewer", "reviewer"]})

    if any(marker in lower for marker in science_markers) and (has_question or word_count >= 10):
        return ToolDecision("specialized_agent", 0.9, "run", {"roles": ["science_expert", "reviewer"]})

    if any(marker in lower for marker in technical_markers) and has_question and word_count >= 10:
        return ToolDecision("specialized_agent", 0.86, "run", {"roles": ["planner", "executor", "reviewer"]})

    if any(marker in lower for marker in planning_markers) and word_count >= 8:
        return ToolDecision("specialized_agent", 0.84, "run", {"roles": ["planner", "executor", "reviewer"]})

    return None


def _parse(raw: str) -> ToolDecision:
    """Parse LLM JSON response into ToolDecision."""
    raw = raw.strip()

    # Strip markdown fences
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    # Find the first complete JSON object (non-greedy to avoid extra data errors)
    match = re.search(r"\{[^{}]*\}", raw, re.DOTALL)
    if not match:
        return _FALLBACK

    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        # Try the whole string as a last resort
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return _FALLBACK

    tool = data.get("tool", "chat")
    if tool not in TOOLS:
        tool = "chat"

    return ToolDecision(
        tool=tool,
        confidence=float(data.get("confidence", 0.5)),
        action=str(data.get("action", "")),
        params=dict(data.get("params", {})),
        raw=raw,
    )
