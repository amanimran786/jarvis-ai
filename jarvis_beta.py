"""
jarvis_beta.py — headless beta test harness for Jarvis.

Bypasses macOS-only imports (overlay, PyQt6) and runs the real pipeline:
  query → interview_profile routing → semantic_memory retrieval → GPT call

Usage:
  python3 jarvis_beta.py
"""

import sys
import os
from pathlib import Path

# Works whether run from inside the repo or from anywhere else
JARVIS_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(JARVIS_ROOT))
os.chdir(str(JARVIS_ROOT))

from dotenv import load_dotenv
load_dotenv(dotenv_path=JARVIS_ROOT / ".env")

import memory as mem
import semantic_memory as smem
import interview_profile as ip
from config import SYSTEM_PROMPT, KB_ROOT
from brain import ask_stream   # OpenAI GPT streaming

# ── Context assembly ──────────────────────────────────────────────────────────

def _build_context(query: str) -> str:
    """
    Assemble context from:
      1. Core facts from memory.json
      2. Semantic KB hits from memory/
      3. Interview profile answer (if interview-related)
    """
    parts = []

    # Core memory facts (top 10)
    data = mem.load()
    facts = data.get("facts", [])[:10]
    if facts:
        parts.append("[Who Aman is]\n" + "\n".join(f"- {f}" for f in facts))

    # Semantic KB hits
    kb_ctx = smem.context_for_query(query, top_k=3, max_chars=1200)
    if kb_ctx:
        parts.append(kb_ctx)

    # Interview profile — loads if query is career/interview related
    ip_answer = ip.answer_for_query(query)
    if ip_answer and len(ip_answer) > 80:
        parts.append(f"[Jarvis knowledge base — career context]\n{ip_answer}")

    return "\n\n".join(parts)


def _route_label(query: str) -> str:
    """Simple display-only routing label."""
    q = query.lower()
    if any(w in q for w in ["interview", "behavioral", "tell me about yourself", "why youtube", "calibration", "story", "weakness", "strength"]):
        return "InterviewIntel"
    if any(w in q for w in ["sql", "python", "code", "debug", "system design", "architecture"]):
        return "TechAssist"
    if any(w in q for w in ["career", "job", "apply", "resume", "role", "search", "jarvis"]):
        return "CareerOS"
    if any(w in q for w in ["strategy", "should i", "plan", "prioritize", "next", "focus"]):
        return "StrategyOS"
    return "General"


# ── REPL ─────────────────────────────────────────────────────────────────────

BANNER = """
╔══════════════════════════════════════════════════╗
║  JARVIS BETA — live pipeline test                ║
║  Model: GPT-4o  |  Memory: 62 facts + 18 KB     ║
║  Type your query. /quit to exit. /status to check║
╚══════════════════════════════════════════════════╝
"""

def run():
    print(BANNER)
    history = []

    while True:
        try:
            raw = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break

        if not raw:
            continue

        if raw.lower() in ("/quit", "/exit", "quit", "exit"):
            print("Exiting.")
            break

        if raw.lower() == "/status":
            s = smem.status()
            data = mem.load()
            print(f"  semantic_memory: {s['entries_indexed']} entries ({s['semantic_entries']} semantic, {s['episodic_entries']} episodic)")
            print(f"  memory.json: {len(data['facts'])} facts, {len(data['conversation_history'])} history entries")
            print(f"  model: GPT-4o via OpenAI")
            continue

        if raw.lower() == "/memory":
            data = mem.load()
            print("  Top facts:")
            for f in data["facts"][:10]:
                print(f"    - {f}")
            continue

        # ── Route + assemble context ────────────────────────────────
        module = _route_label(raw)
        context = _build_context(raw)

        system = SYSTEM_PROMPT
        if context:
            system = system + f"\n\n{context}"

        # Add to history
        history.append({"role": "user", "content": raw})

        # ── Model call ──────────────────────────────────────────────
        print(f"\nJarvis [{module}]: ", end="", flush=True)
        full_response = ""
        try:
            for chunk in ask_stream(system, list(history)):
                print(chunk, end="", flush=True)
                full_response += chunk
            print()
        except Exception as e:
            print(f"\n[ERROR: {e}]")
            history.pop()
            continue

        history.append({"role": "assistant", "content": full_response})

        # Keep history bounded
        if len(history) > 16:
            history = history[-16:]


if __name__ == "__main__":
    run()
