"""
Self-learning engine for Jarvis.

After every conversation:
- Extracts facts, preferences, goals, skills, and projects automatically
- Builds a growing user profile without the user needing to say "remember"

Background knowledge feed:
- Fetches news and updates every few hours on topics the user cares about
- Keeps Jarvis current on the world so it can proactively share relevant info

Reflection:
- Periodically synthesizes everything learned into a coherent user model
"""

import json
import os
import threading
import time
from datetime import datetime, timezone

from brain_claude import ask_claude
from config import HAIKU
from tools import web_search
import memory as mem

KNOWLEDGE_FILE = os.path.join(os.path.dirname(__file__), "knowledge.json")
FEED_INTERVAL_HOURS = 4  # how often to refresh the knowledge feed


# ── Knowledge store ───────────────────────────────────────────────────────────

def _load_knowledge() -> dict:
    if not os.path.exists(KNOWLEDGE_FILE):
        return {
            "user_profile": {},
            "insights": [],
            "knowledge_feed": [],
            "last_feed_update": None
        }
    with open(KNOWLEDGE_FILE) as f:
        return json.load(f)


def _save_knowledge(data: dict) -> None:
    data["last_updated"] = str(datetime.now().strftime("%Y-%m-%d %H:%M"))
    with open(KNOWLEDGE_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ── Auto-extract from conversation ────────────────────────────────────────────

def extract_and_learn(conversation: list[str]) -> None:
    """
    After a conversation, use Claude Haiku to extract what was learned
    and automatically update memory. No user action required.
    """
    if len(conversation) < 2:
        return

    transcript = "\n".join(conversation[-12:])
    existing_facts = mem.list_facts()
    existing_str = "\n".join(f"- {f}" for f in existing_facts) if existing_facts else "None yet."

    prompt = f"""Analyze this conversation between a user and their AI assistant called Jarvis.

Existing known facts:
{existing_str}

Conversation:
{transcript}

Extract ONLY new information not already known. Return a JSON object with these keys (use empty lists/strings if nothing found):
{{
  "new_facts": ["fact about the user"],
  "preferences": {{"key": "value"}},
  "goals": ["user goal or intention mentioned"],
  "projects": [{{"name": "project name", "description": "what they're building"}}],
  "skills": ["skill or knowledge area mentioned"],
  "insights": ["interesting pattern or insight about the user"]
}}

Be specific. Only include things clearly stated or strongly implied. Return valid JSON only."""

    try:
        response = ask_claude(prompt, model=HAIKU)
        # Extract JSON from response
        start = response.find("{")
        end = response.rfind("}") + 1
        if start == -1 or end == 0:
            return
        data = json.loads(response[start:end])

        # Save new facts
        for fact in data.get("new_facts", []):
            if fact and len(fact) > 5:
                mem.add_fact(fact)
                print(f"[Learner] New fact: {fact}")

        # Save preferences
        for k, v in data.get("preferences", {}).items():
            mem.set_preference(k, str(v))
            print(f"[Learner] Preference: {k} = {v}")

        # Save projects
        for p in data.get("projects", []):
            if isinstance(p, dict) and p.get("name"):
                mem.add_project(p["name"], description=p.get("description", ""))
                print(f"[Learner] Project: {p['name']}")

        # Save skills and goals as facts
        for skill in data.get("skills", []):
            fact = f"knows {skill}"
            mem.add_fact(fact)

        for goal in data.get("goals", []):
            if goal and len(goal) > 5:
                mem.add_fact(f"goal: {goal}")

        # Save insights to knowledge store
        insights = data.get("insights", [])
        if insights:
            kdata = _load_knowledge()
            kdata["insights"].extend(insights)
            kdata["insights"] = kdata["insights"][-20:]  # keep last 20
            _save_knowledge(kdata)

    except Exception as e:
        print(f"[Learner] Extraction error: {e}")


# ── Background knowledge feed ─────────────────────────────────────────────────

def _should_refresh_feed() -> bool:
    kdata = _load_knowledge()
    last = kdata.get("last_feed_update")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        hours_since = (datetime.now() - last_dt).total_seconds() / 3600
        return hours_since >= FEED_INTERVAL_HOURS
    except Exception:
        return True


def refresh_knowledge_feed() -> None:
    """Fetch fresh news/updates on topics the user cares about."""
    if not _should_refresh_feed():
        return

    top_topics = mem.get_top_topics(5)
    facts = mem.list_facts()

    # Build search topics from user interests + explicit facts
    search_topics = list(top_topics)

    # Add tech/career interests from facts
    for fact in facts:
        lower = fact.lower()
        for kw in ["python", "javascript", "ai", "machine learning", "startup",
                   "crypto", "finance", "design", "marketing", "fitness"]:
            if kw in lower and kw not in search_topics:
                search_topics.append(kw)

    if not search_topics:
        search_topics = ["artificial intelligence", "technology"]

    kdata = _load_knowledge()
    feed = []

    for topic in search_topics[:4]:  # limit to 4 searches
        try:
            results = web_search(f"latest news {topic} 2025", max_results=2)
            if results and "couldn't find" not in results:
                # Summarize with Haiku (cheap)
                summary = ask_claude(
                    f"Summarize these news results about '{topic}' in 1-2 sentences for a voice assistant:\n{results}",
                    model=HAIKU
                )
                feed.append({"topic": topic, "summary": summary,
                             "fetched": str(datetime.now().strftime("%Y-%m-%d %H:%M"))})
                print(f"[Feed] Updated: {topic}")
        except Exception as e:
            print(f"[Feed] Error fetching {topic}: {e}")

    kdata["knowledge_feed"] = feed
    kdata["last_feed_update"] = str(datetime.now().isoformat())
    _save_knowledge(kdata)


def start_background_feed() -> None:
    """Run knowledge feed refresh in background every FEED_INTERVAL_HOURS hours."""
    def _loop():
        while True:
            try:
                refresh_knowledge_feed()
            except Exception as e:
                print(f"[Feed] Background error: {e}")
            time.sleep(FEED_INTERVAL_HOURS * 3600)

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()
    print("[Learner] Background knowledge feed started.")


# ── Daily reflection ──────────────────────────────────────────────────────────

def reflect() -> str:
    """
    Synthesize everything Jarvis knows into a coherent user model.
    Called once a day — makes Jarvis progressively smarter.
    Only runs when there's enough real data to synthesize.
    """
    facts = mem.list_facts()
    prefs = mem.get_all_preferences()
    projects = mem.get_projects()
    recent_convos = mem.get_recent_conversations(10)
    kdata = _load_knowledge()
    insights = kdata.get("insights", [])

    # Need at least some real data before reflecting
    real_facts = [f for f in facts if len(f) > 10 and f != "my name"]
    if len(real_facts) < 2 and len(recent_convos) < 2:
        print("[Learner] Not enough data yet for reflection — skipping.")
        return ""

    profile_parts = []
    if facts:
        profile_parts.append("Known facts:\n" + "\n".join(f"- {f}" for f in facts))
    if prefs:
        profile_parts.append("Preferences:\n" + "\n".join(f"- {k}: {v}" for k, v in prefs.items()))
    if projects:
        profile_parts.append("Projects:\n" + "\n".join(f"- {p['name']}: {p.get('description','')}" for p in projects))
    if recent_convos:
        profile_parts.append("Recent conversations:\n" + "\n".join(f"- {c['summary']}" for c in recent_convos))
    if insights:
        profile_parts.append("Insights:\n" + "\n".join(f"- {i}" for i in insights[-5:]))

    prompt = f"""Based on everything known about this user, write a short, dense user profile (3-4 sentences)
that captures who they are, what they're working on, their communication style, and how to best help them.
This will be used as context for an AI assistant.

{chr(10).join(profile_parts)}

Return only the profile paragraph, no labels or headers."""

    try:
        profile = ask_claude(prompt, model=HAIKU)
        kdata["user_profile"]["synthesis"] = profile
        kdata["user_profile"]["last_reflection"] = str(datetime.now().strftime("%Y-%m-%d"))
        _save_knowledge(kdata)
        print(f"[Learner] Reflection complete: {profile[:80]}...")
        return profile
    except Exception as e:
        print(f"[Learner] Reflection error: {e}")
        return ""


# ── Get full context for system prompt ────────────────────────────────────────

def get_learning_context() -> str:
    """Return everything learned as context to inject into system prompt."""
    kdata = _load_knowledge()
    parts = []

    # Synthesized user profile
    synthesis = kdata.get("user_profile", {}).get("synthesis")
    if synthesis:
        parts.append(f"User profile:\n{synthesis}")

    # Recent knowledge feed
    feed = kdata.get("knowledge_feed", [])
    if feed:
        items = "\n".join(f"- [{f['topic']}] {f['summary']}" for f in feed[-4:])
        parts.append(f"Recent news in areas the user cares about:\n{items}")

    # Recent insights
    insights = kdata.get("insights", [])
    if insights:
        parts.append("Behavioral insights:\n" + "\n".join(f"- {i}" for i in insights[-3:]))

    return "\n\n" + "\n\n".join(parts) if parts else ""
