from __future__ import annotations

from typing import Any

import browser
import hardware as hw
import terminal

from desktop import bridge


def refresh_nearby_devices(timeout: float = 1.2) -> dict[str, Any]:
    snapshot = hw.discover_nearby(timeout=timeout)
    snapshot["bridge"] = bridge.refresh_bridge_status()
    return snapshot


def open_device_settings(target: str) -> dict[str, Any]:
    message = hw.open_system_settings(target)
    return {
        "ok": message.startswith("Opened "),
        "target": (target or "").strip().lower(),
        "message": message,
    }


def copy_bridge_url() -> str:
    url = bridge.primary_bridge_url()
    if not url:
        return "No bridge URL is available yet."
    terminal.set_clipboard(url)
    return f"Copied {url}"


def copy_current_page_url() -> str:
    info = browser.get_current_page_info()
    if not info.get("ok") or not info.get("url"):
        return info.get("error", "Couldn't read the current browser page.")
    terminal.set_clipboard(info["url"])
    title = info.get("title") or "current page"
    return f"Copied the URL for {title}."


def nearby_summary(timeout: float = 1.2) -> dict[str, Any]:
    snapshot = refresh_nearby_devices(timeout=timeout)
    bluetooth = snapshot.get("bluetooth", {})
    network = snapshot.get("network", {}).get("services", {})
    bridge_snapshot = snapshot.get("bridge", {})
    return {
        "bridge": bridge_snapshot,
        "bluetooth": bluetooth,
        "network": network,
    }
