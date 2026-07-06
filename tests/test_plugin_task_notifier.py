import json
from unittest.mock import patch

from plugins.loader import PluginLoader
from plugins.task_notifier import TaskNotifierPlugin


def test_enabled_task_notifier_loads_without_blocking_other_plugins():
    loader = PluginLoader()

    with patch.object(loader, "_save_registry"):
        plugins = loader.load_all()

    enabled = {plugin.name for plugin in plugins if plugin.enabled}
    assert "task_notifier" in enabled
    assert "auto_tagger" in enabled


def test_task_notifier_dashboard_escapes_event_content(tmp_path):
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    plugin = TaskNotifierPlugin(tmp_path)
    plugin.on_load()
    plugin._log.write_text(
        json.dumps(
            {
                "ts": "2026-07-05T12:00:00Z",
                "event": "failed",
                "title": "<script>alert('xss')</script>",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    widget = plugin.dashboard_widget()

    assert "failed" in widget
    assert "<script>" not in widget
    assert "&lt;script&gt;" in widget
