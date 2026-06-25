"""
local_browser.py — lightweight CDP (Chrome DevTools Protocol) browser harness.

Connects to Chrome's remote debugging port (--remote-debugging-port=9222) and
provides headless page fetching without subprocess overhead. Replaces the
"open Chrome subprocess" web search path in router.py.

Self-healing skills: learns working CSS selectors for article extraction
and persists them to ~/.jarvis_browser_skills.json, improving extraction
over time per domain.

Architecture:
  - is_available(): Check if Chrome debug port is reachable
  - BrowserSession: CDP connection lifecycle (navigate, get_text, get_html, find_text)
  - get_article_text(url): Skills-first extraction with fallback
  - fetch_page(url): One-shot convenience function
"""

from __future__ import annotations
import logging

import base64
import json
import os
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

CHROME_DEBUG_PORT = 9222
CHROME_DEBUG_URL = f"http://localhost:{CHROME_DEBUG_PORT}"
SKILLS_FILE = os.path.expanduser("~/.jarvis_browser_skills.json")


def is_available() -> bool:
    """Return True if Chrome remote debug port is reachable."""
    try:
        req = urllib.request.Request(f"{CHROME_DEBUG_URL}/json/list")
        with urllib.request.urlopen(req, timeout=2) as response:
            return response.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return False
    except Exception:
        return False


def fetch_page(url: str, wait_ms: int = 2000) -> str:
    """One-shot: open url, get visible text, close session."""
    if not is_available():
        return ""
    session = BrowserSession()
    try:
        if not session.navigate(url, wait_ms):
            return ""
        return session.get_text()
    finally:
        session.close()


class BrowserSession:
    """Manages a single Chrome DevTools Protocol connection."""

    def __init__(self, debug_port: int = CHROME_DEBUG_PORT):
        self.debug_port = debug_port
        self.debug_url = f"http://localhost:{debug_port}"
        self.tab_id: Optional[str] = None
        self.ws_socket: Optional[socket.socket] = None
        self.ws_lock = threading.Lock()
        self._next_msg_id = 1
        self._skills = self._load_skills()

    def _load_skills(self) -> dict:
        """Load site-specific selectors from ~/.jarvis_browser_skills.json."""
        if not os.path.exists(SKILLS_FILE):
            return {}
        try:
            with open(SKILLS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_skill(self, domain: str, selector: str, description: str) -> None:
        """Persist a working selector for future use on this domain."""
        self._skills[domain] = {
            "selector": selector,
            "description": description,
            "timestamp": time.time(),
        }
        try:
            os.makedirs(os.path.dirname(SKILLS_FILE), exist_ok=True)
            with open(SKILLS_FILE, "w") as f:
                json.dump(self._skills, f, indent=2)
        except Exception:
            logging.debug("[LocalBrowser] silent failure in _save_skill", exc_info=True)

    def _get_or_open_tab(self) -> bool:
        """Get active tab ID, or open a new one. Return True on success."""
        if self.tab_id and self.ws_socket:
            return True

        try:
            req = urllib.request.Request(f"{self.debug_url}/json/list")
            with urllib.request.urlopen(req, timeout=2) as response:
                targets = json.loads(response.read().decode("utf-8"))
            if not targets:
                # No tabs; open one
                req = urllib.request.Request(f"{self.debug_url}/json/new?about:blank")
                with urllib.request.urlopen(req, timeout=2) as response:
                    tab = json.loads(response.read().decode("utf-8"))
                    self.tab_id = tab.get("id")
                    ws_url = tab.get("webSocketDebuggerUrl")
            else:
                # Use first tab
                tab = targets[0]
                self.tab_id = tab.get("id")
                ws_url = tab.get("webSocketDebuggerUrl")

            if not self.tab_id or not ws_url:
                return False

            # Open WebSocket to tab
            return self._open_websocket(ws_url)
        except Exception:
            return False

    def _open_websocket(self, ws_url: str) -> bool:
        """Open WebSocket connection via minimal handshake. Return True on success."""
        try:
            # Parse ws://host:port/path
            url_parts = ws_url.replace("ws://", "").replace("wss://", "")
            host_port, path = url_parts.split("/", 1)
            host, port = host_port.split(":")
            port = int(port)

            self.ws_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.ws_socket.connect((host, port))

            # HTTP Upgrade handshake
            key = base64.b64encode(os.urandom(16)).decode("utf-8")
            handshake = (
                f"GET /{path} HTTP/1.1\r\n"
                f"Host: {host_port}\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n"
                f"\r\n"
            )
            self.ws_socket.sendall(handshake.encode())

            # Read response (discard headers, assume success)
            response = b""
            while b"\r\n\r\n" not in response:
                chunk = self.ws_socket.recv(1024)
                if not chunk:
                    return False
                response += chunk

            return True
        except Exception:
            if self.ws_socket:
                self.ws_socket.close()
                self.ws_socket = None
            return False

    def _send_command(self, method: str, params: dict | None = None) -> dict | None:
        """Send CDP command via WebSocket; return response or None on error."""
        if not self.ws_socket:
            if not self._get_or_open_tab():
                return None

        msg_id = self._next_msg_id
        self._next_msg_id += 1

        payload = {
            "id": msg_id,
            "method": method,
            "params": params or {},
        }
        body = json.dumps(payload).encode("utf-8")

        try:
            with self.ws_lock:
                # WebSocket frame: FIN=1, opcode=1 (text), mask=True
                frame = self._make_frame(body)
                self.ws_socket.sendall(frame)

                # Read response frame(s) until we match our message ID
                response_text = self._read_frame()
                if not response_text:
                    return None

                response = json.loads(response_text)
                if response.get("id") == msg_id:
                    return response
                return None
        except Exception:
            return None

    def _make_frame(self, data: bytes) -> bytes:
        """Create WebSocket frame with mask (required for client)."""
        mask = os.urandom(4)
        payload = bytearray(data)
        for i in range(len(payload)):
            payload[i] ^= mask[i % 4]

        frame = bytearray()
        frame.append(0x81)  # FIN=1, opcode=1 (text)

        length = len(payload)
        if length < 126:
            frame.append(0x80 | length)  # mask=1, payload length
        elif length < 65536:
            frame.append(0xFE)  # mask=1, 16-bit length
            frame.extend(struct.pack("!H", length))
        else:
            frame.append(0xFF)  # mask=1, 64-bit length
            frame.extend(struct.pack("!Q", length))

        frame.extend(mask)
        frame.extend(payload)

        return bytes(frame)

    def _read_frame(self, timeout: float = 5.0) -> str | None:
        """Read one WebSocket frame and return text payload."""
        try:
            self.ws_socket.settimeout(timeout)
            header = self.ws_socket.recv(2)
            if len(header) < 2:
                return None

            byte0, byte1 = header[0], header[1]
            fin = (byte0 & 0x80) != 0
            opcode = byte0 & 0x0F
            has_mask = (byte1 & 0x80) != 0
            payload_len = byte1 & 0x7F

            if payload_len == 126:
                payload_len = struct.unpack("!H", self.ws_socket.recv(2))[0]
            elif payload_len == 127:
                payload_len = struct.unpack("!Q", self.ws_socket.recv(8))[0]

            if has_mask:
                mask = self.ws_socket.recv(4)
            else:
                mask = None

            payload = b""
            while len(payload) < payload_len:
                chunk = self.ws_socket.recv(payload_len - len(payload))
                if not chunk:
                    return None
                payload += chunk

            if mask:
                payload = bytearray(payload)
                for i in range(len(payload)):
                    payload[i] ^= mask[i % 4]
                payload = bytes(payload)

            if opcode == 1:  # text frame
                return payload.decode("utf-8", errors="ignore")
            return None
        except Exception:
            return None

    def navigate(self, url: str, wait_ms: int = 2000) -> bool:
        """Navigate to url and wait for Page.loadEventFired. Return True on success."""
        if not self._get_or_open_tab():
            return False

        resp = self._send_command("Page.navigate", {"url": url})
        if not resp or "frameId" not in resp.get("result", {}):
            return False

        # Wait for load event (naive: sleep; better would be event listener)
        time.sleep(wait_ms / 1000.0)
        return True

    def get_text(self) -> str:
        """Extract visible text via document.body.innerText."""
        if not self._get_or_open_tab():
            return ""

        resp = self._send_command(
            "Runtime.evaluate",
            {
                "expression": "document.body.innerText",
                "returnByValue": True,
            },
        )
        if not resp or "result" not in resp.get("result", {}):
            return ""

        result = resp["result"]["result"]
        return result.get("value", "")

    def get_html(self) -> str:
        """Extract full HTML via document.documentElement.outerHTML."""
        if not self._get_or_open_tab():
            return ""

        resp = self._send_command(
            "Runtime.evaluate",
            {
                "expression": "document.documentElement.outerHTML",
                "returnByValue": True,
            },
        )
        if not resp or "result" not in resp.get("result", {}):
            return ""

        result = resp["result"]["result"]
        return result.get("value", "")

    def find_text(self, selector: str) -> list[str]:
        """Execute querySelectorAll and return textContent of matches."""
        if not self._get_or_open_tab():
            return []

        expr = f"""
        Array.from(document.querySelectorAll('{selector}')).map(el => el.textContent)
        """
        resp = self._send_command(
            "Runtime.evaluate",
            {
                "expression": expr,
                "returnByValue": True,
            },
        )
        if not resp or "result" not in resp.get("result", {}):
            return []

        result = resp["result"]["result"]
        return result.get("value", [])

    def get_article_text(self, url: str) -> str:
        """
        Extract article text: try known skill first, then fallback to get_text(),
        and save working approach.
        """
        if not self.navigate(url):
            return ""

        # Compute domain from URL for skill lookup
        try:
            from urllib.parse import urlparse

            domain = urlparse(url).netloc or "default"
        except Exception:
            domain = "default"

        # Try stored skill first
        if domain in self._skills:
            skill = self._skills[domain]
            selector = skill.get("selector")
            if selector:
                texts = self.find_text(selector)
                if texts:
                    result = "\n".join(texts)
                    if result.strip():
                        return result

        # Fallback: get all visible text
        text = self.get_text()
        if text.strip():
            # Save this approach for future use
            self._save_skill(domain, "body", "fallback: all text")
        return text

    def close(self) -> None:
        """Close WebSocket and clean up."""
        if self.ws_socket:
            try:
                self.ws_socket.close()
            except Exception:
                logging.debug("[LocalBrowser] silent failure in close", exc_info=True)
            self.ws_socket = None
        self.tab_id = None
