"""Jarvis Plugin Loader."""
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
                if r: msg = r + "\n" + msg
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
        return "\n".join(lines)
