#!/usr/bin/env python3
"""CODEX-7: Install the Jarvis Plugin System."""
import pathlib, json

BASE = pathlib.Path.home() / "jarvis-ai"
PLUGINS = BASE / "plugins"
PLUGINS.mkdir(exist_ok=True)

# ── plugins/__init__.py ──────────────────────────────────────────────
(PLUGINS / "__init__.py").write_text('"""Jarvis Plugins Package."""\n')
print("Created plugins/__init__.py")

# ── plugins/base.py ─────────────────────────────────────────────────
(PLUGINS / "base.py").write_text('''"""Jarvis Plugin Base Class — all plugins inherit from JarvisPlugin."""
from abc import ABC
from typing import Optional, Dict, Any
from pathlib import Path
import json

class JarvisPlugin(ABC):
    """
    Base class for all Jarvis plugins.
    Override only the hooks you need. All methods are optional.

    Lifecycle:  on_load -> [hooks...] -> on_unload
    Task hooks: on_task_queued -> on_task_started -> on_task_completed | on_task_failed
    Msg hooks:  on_message, on_response
    Dashboard:  dashboard_widget()
    """
    name: str = "unnamed_plugin"
    version: str = "0.1.0"
    description: str = "No description provided."
    author: str = "unknown"
    enabled: bool = True

    def __init__(self, jarvis_base: Path):
        self.base = jarvis_base
        self.config_path = jarvis_base / "plugins" / f"{self.name}.config.json"
        self._config: Dict[str, Any] = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        try:
            return json.loads(self.config_path.read_text())
        except Exception:
            return {}

    def save_config(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(self._config, indent=2))

    def get(self, key: str, default=None):
        return self._config.get(key, default)

    def set(self, key: str, value) -> None:
        self._config[key] = value
        self.save_config()

    def on_load(self) -> None: pass
    def on_unload(self) -> None: pass
    def on_task_queued(self, task: Dict[str, Any]) -> Optional[Dict[str, Any]]: return None
    def on_task_started(self, task: Dict[str, Any]) -> None: pass
    def on_task_completed(self, task: Dict[str, Any]) -> None: pass
    def on_task_failed(self, task: Dict[str, Any]) -> None: pass
    def on_message(self, msg: str, ctx: Dict[str, Any]) -> Optional[str]: return None
    def on_response(self, resp: str, ctx: Dict[str, Any]) -> Optional[str]: return None
    def dashboard_widget(self) -> Optional[str]: return None

    def __repr__(self) -> str:
        return f"<JarvisPlugin {self.name} v{self.version} {'OK' if self.enabled else 'OFF'}>"
''')
print("Created plugins/base.py")

# ── plugins/loader.py ────────────────────────────────────────────────
(PLUGINS / "loader.py").write_text('''"""Jarvis Plugin Loader."""
import importlib.util, traceback, json, sys
from pathlib import Path
from typing import List, Dict

PLUGINS_DIR = Path(__file__).parent
BASE_DIR = PLUGINS_DIR.parent
REGISTRY_PATH = PLUGINS_DIR / "registry.json"
EXCLUDES = {"__init__.py", "base.py", "loader.py"}

def _base():
    sys.path.insert(0, str(BASE_DIR))
    from plugins.base import JarvisPlugin
    return JarvisPlugin

class PluginLoader:
    def __init__(self, base: Path = BASE_DIR):
        self.base = base
        self.plugins = []
        self._registry: Dict[str, bool] = self._load_registry()

    def _load_registry(self):
        try: return json.loads(REGISTRY_PATH.read_text())
        except: return {}

    def _save_registry(self):
        REGISTRY_PATH.write_text(json.dumps({p.name: p.enabled for p in self.plugins}, indent=2))

    def discover(self):
        return [p for p in PLUGINS_DIR.glob("*.py") if p.name not in EXCLUDES]

    def load_all(self):
        JarvisPlugin = _base()
        loaded = []
        for path in self.discover():
            p = self._load_file(path, JarvisPlugin)
            if p: loaded.append(p)
        self.plugins = loaded
        self._save_registry()
        return loaded

    def _load_file(self, path, JarvisPlugin):
        try:
            spec = importlib.util.spec_from_file_location(path.stem, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            for name in dir(mod):
                attr = getattr(mod, name)
                if isinstance(attr, type) and issubclass(attr, JarvisPlugin) and attr is not JarvisPlugin:
                    inst = attr(self.base)
                    if inst.name in self._registry:
                        inst.enabled = self._registry[inst.name]
                    inst.on_load()
                    return inst
        except Exception:
            print(f"  Warning: failed to load {path.name}")
            traceback.print_exc()
        return None

    def get_enabled(self): return [p for p in self.plugins if p.enabled]

    def enable(self, name):
        for p in self.plugins:
            if p.name == name:
                p.enabled = True; self._save_registry(); return True
        return False

    def disable(self, name):
        for p in self.plugins:
            if p.name == name:
                p.enabled = False; p.on_unload(); self._save_registry(); return True
        return False

    def fire_task_queued(self, task):
        for p in self.get_enabled():
            try:
                r = p.on_task_queued(task)
                if r is not None: task = r
            except: pass
        return task

    def fire_task_completed(self, task):
        for p in self.get_enabled():
            try: p.on_task_completed(task)
            except: pass

    def fire_task_failed(self, task):
        for p in self.get_enabled():
            try: p.on_task_failed(task)
            except: pass

    def fire_message(self, msg, ctx):
        for p in self.get_enabled():
            try:
                r = p.on_message(msg, ctx)
                if r: msg = r + "\\n" + msg
            except: pass
        return msg

    def collect_dashboard_widgets(self):
        out = []
        for p in self.get_enabled():
            try:
                w = p.dashboard_widget()
                if w: out.append(w)
            except: pass
        return out

    def summary(self):
        lines = [f"Jarvis Plugin System — {len(self.plugins)} plugin(s)"]
        for p in self.plugins:
            s = "OK" if p.enabled else "--"
            lines.append(f"  [{s}] {p.name} v{p.version}  {p.description}")
        return "\\n".join(lines)
''')
print("Created plugins/loader.py")

# ── plugins/task_notifier.py ─────────────────────────────────────────
(PLUGINS / "task_notifier.py").write_text('''"""task_notifier plugin — logs task completions/failures to JSONL."""
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
            f.write(json.dumps(entry) + "\\n")

    def on_task_completed(self, task: Dict[str, Any]): self._write("completed", task)
    def on_task_failed(self, task: Dict[str, Any]): self._write("failed", task)

    def dashboard_widget(self):
        try:
            lines = self._log.read_text().splitlines()[-10:]
            events = [json.loads(l) for l in lines if l.strip()]
            rows = "".join(
                f"<tr><td style=\\'color:#888;font-size:0.78em\\'>{e[\\'ts\\'][:16]}</td>"
                f"<td style=\\'color:#66bb6a\\'>completed</td>"
                f"<td style=\\'color:#ccc\\'>{str(e.get(\\'title\\',\\'?\\'))[:45]}</td></tr>"
                for e in reversed(events))
            return (f"<div><p style=\\'color:#4fc3f7;margin:4px 0\\'>Task Log (last {len(events)})</p>"
                    f"<table style=\\'width:100%;font-size:0.82em\\'>{rows}</table></div>")
        except:
            return "<div style=\\'color:#888\\'>TaskNotifier: no events yet</div>"
''')
print("Created plugins/task_notifier.py")

# ── plugins/auto_tagger.py ───────────────────────────────────────────
(PLUGINS / "auto_tagger.py").write_text('''"""auto_tagger plugin — auto-assigns domain tags to tasks by keyword."""
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
            f"<span style=\\'background:#1e3a4a;color:#4fc3f7;padding:2px 7px;"
            f"border-radius:10px;font-size:0.78em\\'>{d}({n})</span>"
            for d, n in sorted(c.items()))
        return (f"<div><p style=\\'color:#4fc3f7;margin:4px 0\\'>AutoTagger domains</p>"
                f"<div style=\\'margin:4px 0\\'>{pills}</div></div>")
''')
print("Created plugins/auto_tagger.py")

# ── plugin_manager.py ────────────────────────────────────────────────
(BASE / "plugin_manager.py").write_text('''#!/usr/bin/env python3
"""Jarvis Plugin Manager CLI
Usage:
  python3 plugin_manager.py           # list all plugins
  python3 plugin_manager.py test      # fire test events
  python3 plugin_manager.py enable <name>
  python3 plugin_manager.py disable <name>
"""
import sys
from pathlib import Path
BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
from plugins.loader import PluginLoader

def main():
    loader = PluginLoader(BASE)
    loader.load_all()
    print()
    print("=" * 60)
    print(loader.summary())
    print("=" * 60)

    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"

    if cmd == "enable" and len(sys.argv) > 2:
        name = sys.argv[2]
        print(f"{'Enabled' if loader.enable(name) else 'Not found'}: {name}")
    elif cmd == "disable" and len(sys.argv) > 2:
        name = sys.argv[2]
        print(f"{'Disabled' if loader.disable(name) else 'Not found'}: {name}")
    elif cmd == "test":
        print()
        print("=== Test Run ===")
        task = {"id": "test-001", "title": "write API documentation", "status": "queued"}
        task = loader.fire_task_queued(task)
        print(f"on_task_queued  => domain={task.get('domain')!r}")
        task["status"] = "done"
        loader.fire_task_completed(task)
        print("on_task_completed => fired (check plugins/task_log.jsonl)")
        widgets = loader.collect_dashboard_widgets()
        print(f"dashboard_widget  => {len(widgets)} widget(s)")
        msg = loader.fire_message("hello jarvis", {})
        print(f"fire_message      => {msg!r}")
        print()
        print("All hooks OK!")

    widgets = loader.collect_dashboard_widgets()
    if widgets:
        print(f"({len(widgets)} dashboard widget(s) registered)")

    input("\\nPress Enter to close...")

if __name__ == "__main__":
    main()
''')
print("Created plugin_manager.py")

# ── registry.json seed ───────────────────────────────────────────────
(PLUGINS / "registry.json").write_text(json.dumps({"task_notifier": True, "auto_tagger": True}, indent=2))
print("Created plugins/registry.json")

print()
print("=" * 60)
print("CODEX-7 COMPLETE — Jarvis Plugin System installed!")
print()
print("  plugins/__init__.py")
print("  plugins/base.py         JarvisPlugin base class")
print("  plugins/loader.py       PluginLoader + hook dispatchers")
print("  plugins/task_notifier.py  logs task events to JSONL")
print("  plugins/auto_tagger.py    auto-tags tasks by keyword")
print("  plugin_manager.py       CLI: list / test / enable / disable")
print("=" * 60)
input("\nPress Enter to close...")
