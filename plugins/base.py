"""Jarvis Plugin Base Class — all plugins inherit from JarvisPlugin."""
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
