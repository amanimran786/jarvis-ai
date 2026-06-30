"""task_notifier plugin — logs task completions/failures to JSONL."""
import json, sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Any
sys.path.insert(0, str(Path(__file__).parent.parent))
from plugins.base import JarvisPlugin

class TaskNotifierPlugin(JarvisPlugin):
    name = "task_notifier"
    version = "1.0.0"
    description = "Logs task events to plugins/task_log.jsonl"
    author = "Jarvis Core"

    def on_load(self):
        self._log = self.base / "plugins" / "task_log.jsonl"

    def _write(self, event, task):
        entry = {"ts": datetime.now().isoformat(), "event": event,
                 "id": task.get("id"), "title": task.get("title"),
                 "status": task.get("status"), "domain": task.get("domain")}
        with open(self._log, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def on_task_completed(self, task: Dict[str, Any]): self._write("completed", task)
    def on_task_failed(self, task: Dict[str, Any]): self._write("failed", task)

    def dashboard_widget(self):
        try:
            lines = self._log.read_text().splitlines()[-10:]
            events = [json.loads(l) for l in lines if l.strip()]
            rows = "".join(
                f"<tr><td style=\'color:#888;font-size:0.78em\'>{e[\'ts\'][:16]}</td>"
                f"<td style=\'color:#66bb6a\'>completed</td>"
                f"<td style=\'color:#ccc\'>{str(e.get(\'title\',\'?\'))[:45]}</td></tr>"
                for e in reversed(events))
            return (f"<div><p style=\'color:#4fc3f7;margin:4px 0\'>Task Log (last {len(events)})</p>"
                    f"<table style=\'width:100%;font-size:0.82em\'>{rows}</table></div>")
        except:
            return "<div style=\'color:#888\'>TaskNotifier: no events yet</div>"
