from __future__ import annotations

from typing import Any

import api
import hardware as hw


def refresh_bridge_status() -> dict[str, Any]:
    return hw.bridge_status(api_host=api.get_host(), api_port=api.get_port())


def bridge_urls() -> list[str]:
    return list(refresh_bridge_status().get("urls", []))


def primary_bridge_url() -> str:
    snap = refresh_bridge_status()
    return snap.get("primary_url", "") or ""


def bridge_enabled() -> bool:
    return bool(refresh_bridge_status().get("enabled", False))


def bridge_snapshot() -> dict[str, Any]:
    return refresh_bridge_status()

