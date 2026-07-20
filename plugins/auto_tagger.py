"""auto_tagger plugin — auto-assigns domain tags to tasks by keyword."""
import sys
from pathlib import Path
from typing import Dict, Any, Optional
sys.path.insert(0, str(Path(__file__).parent.parent))
from plugins.base import JarvisPlugin

KEYWORD_MAP = {
    "code": "engineering", "bug": "engineering", "test": "engineering",
    "deploy": "engineering", "api": "engineering", "fix": "engineering",
    "refactor": "engineering", "build": "engineering", "database": "engineering",
    "email": "comms", "slack": "comms", "message": "comms", "notify": "comms",
    "write": "writing", "draft": "writing", "document": "writing",
    "report": "writing", "blog": "writing", "summary": "writing",
    "research": "research", "search": "research", "find": "research",
    "analyze": "research", "investigate": "research", "review": "research",
    "data": "data", "chart": "data", "metrics": "data", "csv": "data",
    "design": "design", "ui": "design", "ux": "design", "figma": "design",
}

class AutoTaggerPlugin(JarvisPlugin):
    name = "auto_tagger"
    version = "1.0.0"
    description = "Auto-assigns domain labels to tasks by title keywords"
    author = "Jarvis Core"

    def on_task_queued(self, task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if task.get("domain"): return None
        title = str(task.get("title", "")).lower()
        for kw, domain in KEYWORD_MAP.items():
            if kw in title:
                task = dict(task)
                task["domain"] = domain
                task.setdefault("_meta", {})["auto_tagged"] = True
                return task
        return None

    def dashboard_widget(self):
        from collections import Counter
        c = Counter(KEYWORD_MAP.values())
        pills = " ".join(
            f"<span style=\'background:#1e3a4a;color:#4fc3f7;padding:2px 7px;"
            f"border-radius:10px;font-size:0.78em\'>{d}({n})</span>"
            for d, n in sorted(c.items()))
        return (f"<div><p style=\'color:#4fc3f7;margin:4px 0\'>AutoTagger domains</p>"
                f"<div style=\'margin:4px 0\'>{pills}</div></div>")
