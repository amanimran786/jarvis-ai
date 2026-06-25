from __future__ import annotations

import os
import subprocess
import threading
import logging
import shutil
import atexit
import time
import re

log = logging.getLogger("jarvis.tunnel")

_active_tunnel_url: str | None = None
_tunnel_proc: subprocess.Popen | None = None
_lock = threading.Lock()


def find_cloudflared_path() -> str | None:
    """Locate the cloudflared executable with priority paths."""
    candidates = [
        "/opt/homebrew/bin/cloudflared",
        "/usr/local/bin/cloudflared",
        os.path.expanduser("~/bin/cloudflared"),
    ]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return shutil.which("cloudflared")


def start_tunnel(port: int) -> str | None:
    """
    Launch a secure Cloudflare Tunnel in the background.
    If JARVIS_CLOUDFLARE_TUNNEL_NAME and JARVIS_CLOUDFLARE_DOMAIN are set in .env,
    starts your permanent named tunnel. Otherwise, launches a dynamic Quick Tunnel.
    """
    global _tunnel_proc, _active_tunnel_url

    with _lock:
        if _active_tunnel_url:
            return _active_tunnel_url

        binary = find_cloudflared_path()
        if not binary:
            log.warning("[Tunnel] cloudflared binary not found. Secure global internet tunnel is disabled.")
            return None

        # Check if the user has configured a named permanent tunnel in their .env
        tunnel_name = os.getenv("JARVIS_CLOUDFLARE_TUNNEL_NAME", "").strip()
        custom_domain = os.getenv("JARVIS_CLOUDFLARE_DOMAIN", "").strip()

        # M5: validate tunnel_name before passing to subprocess — rejects argument injection
        # like "--credentials-file /etc/passwd" masquerading as a tunnel name.
        if tunnel_name and not re.fullmatch(r"[a-zA-Z0-9_-]{1,63}", tunnel_name):
            log.error("[Tunnel] Invalid JARVIS_CLOUDFLARE_TUNNEL_NAME — must be alphanumeric/dash/underscore, max 63 chars. Falling back to Quick Tunnel.")
            tunnel_name = ""

        if tunnel_name and custom_domain:
            log.info(f"[Tunnel] Starting configured permanent Cloudflare Tunnel '{tunnel_name}' for domain '{custom_domain}'")
            try:
                _tunnel_proc = subprocess.Popen(
                    [binary, "tunnel", "run", tunnel_name],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
                # Set active URL to your permanent domain
                if not custom_domain.startswith(("http://", "https://")):
                    _active_tunnel_url = f"https://{custom_domain}"
                else:
                    _active_tunnel_url = custom_domain
                
                log.info(f"[Tunnel] Permanent named tunnel established: {_active_tunnel_url}")
                print(f"[Tunnel] Permanent secure global tunnel established: {_active_tunnel_url}")
                return _active_tunnel_url
            except Exception as e:
                log.error(f"[Tunnel] Failed to spawn named tunnel '{tunnel_name}': {e}. Falling back to Quick Tunnel.")

        log.info(f"[Tunnel] Starting Cloudflare Quick Tunnel to http://127.0.0.1:{port} using {binary}")
        try:
            # Quick tunnels are created by passing the --url flag without tunnel name
            _tunnel_proc = subprocess.Popen(
                [binary, "tunnel", "--url", f"http://127.0.0.1:{port}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except Exception as e:
            log.error(f"[Tunnel] Failed to spawn cloudflared: {e}")
            return None

    # Thread-safe event to signal when the URL is parsed
    parsed_event = threading.Event()

    def _read_stderr():
        global _active_tunnel_url
        url_pattern = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
        
        # cloudflared prints quick tunnel configuration details to stderr
        for line in iter(_tunnel_proc.stderr.readline, ""):
            if not line:
                break
            match = url_pattern.search(line)
            if match:
                with _lock:
                    _active_tunnel_url = match.group(0)
                log.info(f"[Tunnel] Dynamic secure tunnel established: {_active_tunnel_url}")
                print(f"[Tunnel] Dynamic secure global tunnel established: {_active_tunnel_url}")
                parsed_event.set()
        
    t = threading.Thread(target=_read_stderr, daemon=True, name="CloudflaredReader")
    t.start()

    # Wait up to 10 seconds for Cloudflare to assign a dynamic URL
    parsed_event.wait(timeout=10.0)
    return _active_tunnel_url


def get_tunnel_url() -> str | None:
    """Return the active secure tunnel URL if running."""
    with _lock:
        return _active_tunnel_url


def stop_tunnel():
    """Cleanly terminate the Cloudflare subprocess on exit."""
    global _tunnel_proc, _active_tunnel_url
    with _lock:
        if _tunnel_proc:
            log.info("[Tunnel] Stopping Cloudflare Quick Tunnel subprocess...")
            try:
                _tunnel_proc.terminate()
                _tunnel_proc.wait(timeout=2.0)
            except Exception:
                try:
                    _tunnel_proc.kill()
                except Exception:
                    logging.debug("[TunnelManager] silent failure in stop_tunnel", exc_info=True)
            _tunnel_proc = None
        _active_tunnel_url = None


# Ensure clean shutdown on python exit or crash
atexit.register(stop_tunnel)
