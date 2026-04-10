"""
Jarvis Hardware Abstraction Layer.

Provides a unified interface for controlling physical hardware:
  - Serial/USB devices (Arduino, ESP32, Teensy, Raspberry Pi Pico)
  - HTTP/REST devices (ESP32 WiFi, smart relays, custom endpoints)
  - MQTT devices (IoT mesh, home automation)
  - Simulated devices (for testing without hardware)

Protocol: JSON command/response over all transports.
  Command:  {"cmd": "fire", "params": {"duration": 500}}
  Response: {"ok": true, "state": "fired", "msg": "Fired 500ms"}

Usage:
  import hardware as hw
  hw.register(SerialDevice("web_shooter", port="/dev/tty.usbmodem*", baud=9600))
  hw.command("web_shooter", "fire", duration=500)
  hw.status()
"""

import json
import copy
import threading
import time
import glob
import logging
import os
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

log = logging.getLogger("jarvis.hardware")

# ── Command result ─────────────────────────────────────────────────────────────

class CommandResult:
    def __init__(self, ok: bool, msg: str = "", state: str = "", raw: dict = None):
        self.ok    = ok
        self.msg   = msg
        self.state = state
        self.raw   = raw or {}
        self.ts    = datetime.now()

    def __str__(self):
        status = "OK" if self.ok else "FAIL"
        return f"[{status}] {self.msg}" + (f" (state={self.state})" if self.state else "")


# ── Base device ────────────────────────────────────────────────────────────────

class Device(ABC):
    def __init__(self, name: str, description: str = ""):
        self.name        = name
        self.description = description
        self.connected   = False
        self.last_seen:  datetime | None = None
        self._lock       = threading.Lock()
        self._state: dict = {}
        self._history: list[dict] = []

    @abstractmethod
    def connect(self) -> bool: ...

    @abstractmethod
    def disconnect(self): ...

    @abstractmethod
    def send(self, cmd: str, params: dict = None) -> CommandResult: ...

    def get_state(self) -> dict:
        return dict(self._state)

    def _record(self, cmd: str, result: CommandResult):
        self._history.append({
            "ts": result.ts.isoformat(),
            "cmd": cmd,
            "ok": result.ok,
            "msg": result.msg,
        })
        self._history = self._history[-50:]  # keep last 50
        self.last_seen = result.ts

    def status_str(self) -> str:
        conn = "CONNECTED" if self.connected else "DISCONNECTED"
        state = ", ".join(f"{k}={v}" for k, v in self._state.items()) if self._state else "—"
        return f"{self.name} [{conn}] state={state}"


# ── Serial device (Arduino / ESP32 USB / Pico) ────────────────────────────────

class SerialDevice(Device):
    """
    Communicates with microcontrollers over USB serial.
    Protocol: newline-delimited JSON.
      → {"cmd": "fire", "params": {"duration": 500}}\n
      ← {"ok": true, "state": "idle", "msg": "Fired 500ms"}\n
    """

    def __init__(self, name: str, port: str, baud: int = 115200,
                 timeout: float = 3.0, description: str = ""):
        super().__init__(name, description)
        self.port    = port
        self.baud    = baud
        self.timeout = timeout
        self._serial = None

    def _resolve_port(self) -> str | None:
        """Resolve glob patterns like /dev/tty.usbmodem*."""
        if "*" in self.port or "?" in self.port:
            matches = glob.glob(self.port)
            return matches[0] if matches else None
        return self.port

    def connect(self) -> bool:
        try:
            import serial
            port = self._resolve_port()
            if not port:
                log.warning(f"[{self.name}] No port matched: {self.port}")
                return False
            self._serial = serial.Serial(port, self.baud, timeout=self.timeout)
            time.sleep(2)  # allow Arduino bootloader to settle
            self._serial.reset_input_buffer()
            self.connected = True
            log.info(f"[{self.name}] Connected on {port} @ {self.baud}baud")
            return True
        except Exception as e:
            log.error(f"[{self.name}] Serial connect failed: {e}")
            return False

    def disconnect(self):
        if self._serial and self._serial.is_open:
            self._serial.close()
        self.connected = False

    def send(self, cmd: str, params: dict = None) -> CommandResult:
        with self._lock:
            if not self.connected:
                if not self.connect():
                    return CommandResult(False, f"Device {self.name} not connected")

            payload = json.dumps({"cmd": cmd, "params": params or {}}) + "\n"
            try:
                self._serial.write(payload.encode())
                self._serial.flush()

                # Read response with timeout
                raw = self._serial.readline().decode().strip()
                if not raw:
                    result = CommandResult(False, "No response from device")
                else:
                    data = json.loads(raw)
                    ok    = bool(data.get("ok", False))
                    msg   = data.get("msg", "")
                    state = data.get("state", "")
                    self._state.update(data.get("state_map", {}))
                    result = CommandResult(ok, msg, state, data)

            except json.JSONDecodeError:
                result = CommandResult(True, f"Raw: {raw}", raw={"raw": raw})
            except Exception as e:
                self.connected = False
                result = CommandResult(False, str(e))

            self._record(cmd, result)
            return result


# ── HTTP device (ESP32 WiFi / custom REST endpoint) ───────────────────────────

class HttpDevice(Device):
    """
    Communicates with devices over HTTP/REST.
    POST {base_url}/command  →  {"cmd": ..., "params": ...}
    GET  {base_url}/status   →  {"state": {...}}
    """

    def __init__(self, name: str, base_url: str,
                 auth_token: str = "", timeout: float = 5.0, description: str = ""):
        super().__init__(name, description)
        self.base_url   = base_url.rstrip("/")
        self.auth_token = auth_token
        self.timeout    = timeout

    def connect(self) -> bool:
        try:
            import urllib.request
            req = urllib.request.Request(
                f"{self.base_url}/status",
                headers=self._headers()
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
                self._state.update(data.get("state", {}))
            self.connected = True
            return True
        except Exception as e:
            log.warning(f"[{self.name}] HTTP connect failed: {e}")
            return False

    def disconnect(self):
        self.connected = False

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.auth_token:
            h["Authorization"] = f"Bearer {self.auth_token}"
        return h

    def send(self, cmd: str, params: dict = None) -> CommandResult:
        import urllib.request, urllib.error
        with self._lock:
            payload = json.dumps({"cmd": cmd, "params": params or {}}).encode()
            req = urllib.request.Request(
                f"{self.base_url}/command",
                data=payload,
                headers=self._headers(),
                method="POST"
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read())
                    ok    = bool(data.get("ok", True))
                    msg   = data.get("msg", "")
                    state = data.get("state", "")
                    self._state.update(data.get("state_map", {}))
                    result = CommandResult(ok, msg, state, data)
            except urllib.error.HTTPError as e:
                result = CommandResult(False, f"HTTP {e.code}: {e.reason}")
            except Exception as e:
                result = CommandResult(False, str(e))

            self._record(cmd, result)
            return result

    def get_remote_status(self) -> dict:
        """Fetch full state from device."""
        import urllib.request
        try:
            req = urllib.request.Request(
                f"{self.base_url}/status",
                headers=self._headers()
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except Exception:
            return {}


# ── Simulated device (testing / no hardware) ─────────────────────────────────

class SimulatedDevice(Device):
    """
    Fake device that logs commands and returns success.
    Useful for testing hardware logic without physical hardware.
    """

    def __init__(self, name: str, commands: list[str] = None, description: str = ""):
        super().__init__(name, description or f"Simulated {name}")
        self.supported_cmds = set(commands or [])
        self.connected = True

    def connect(self) -> bool:
        self.connected = True
        return True

    def disconnect(self):
        self.connected = False

    def send(self, cmd: str, params: dict = None) -> CommandResult:
        with self._lock:
            if self.supported_cmds and cmd not in self.supported_cmds:
                result = CommandResult(False, f"Unknown command: {cmd}")
            else:
                param_str = f" {params}" if params else ""
                self._state["last_cmd"] = cmd
                self._state["last_params"] = str(params)
                result = CommandResult(True, f"[SIM] {cmd}{param_str} executed", cmd)
                print(f"[HW:SIM:{self.name}] {cmd}{param_str}")
            self._record(cmd, result)
            return result


# ── Device registry ────────────────────────────────────────────────────────────

_devices: dict[str, Device] = {}
_lock = threading.Lock()

# Command log (last 200 entries across all devices)
_command_log: list[dict] = []


def register(device: Device, auto_connect: bool = True) -> bool:
    """Register a device. Optionally attempt connection immediately."""
    with _lock:
        _devices[device.name] = device
    if auto_connect:
        ok = device.connect()
        print(f"[HW] Registered '{device.name}' — {'connected' if ok else 'offline'}")
        return ok
    print(f"[HW] Registered '{device.name}' (not connected yet)")
    return True


def unregister(name: str):
    with _lock:
        device = _devices.pop(name, None)
    if device:
        device.disconnect()


def get(name: str) -> Device | None:
    return _devices.get(name)


def list_devices() -> list[Device]:
    return list(_devices.values())


# ── Command execution ──────────────────────────────────────────────────────────

# Commands blocked for safety (require explicit override)
_BLOCKED_CMDS = {"self_destruct", "emergency_stop_all", "override_safety"}

# Per-command cooldowns to prevent accidental rapid re-fire
_COOLDOWNS: dict[str, float] = {}        # cmd_key → last_fired_time
_COOLDOWN_SECONDS: dict[str, float] = {} # cmd_key → cooldown_duration


def set_cooldown(device_name: str, cmd: str, seconds: float):
    """Set minimum time between identical commands on a device."""
    _COOLDOWN_SECONDS[f"{device_name}:{cmd}"] = seconds


def command(device_name: str, cmd: str, override_safety: bool = False, **params) -> CommandResult:
    """
    Execute a command on a named device.
    Returns CommandResult — always safe to call, never raises.
    """
    # Safety check
    if cmd in _BLOCKED_CMDS and not override_safety:
        return CommandResult(False, f"Command '{cmd}' is blocked. Use override_safety=True.")

    # Cooldown check
    cooldown_key = f"{device_name}:{cmd}"
    cooldown = _COOLDOWN_SECONDS.get(cooldown_key, 0)
    if cooldown > 0:
        last = _COOLDOWNS.get(cooldown_key, 0)
        elapsed = time.time() - last
        if elapsed < cooldown:
            remaining = cooldown - elapsed
            return CommandResult(False, f"Cooldown: wait {remaining:.1f}s before firing again.")
        _COOLDOWNS[cooldown_key] = time.time()

    device = _devices.get(device_name)
    if not device:
        return CommandResult(False, f"Device '{device_name}' not registered.")

    result = device.send(cmd, params if params else None)

    # Log it
    _command_log.append({
        "ts":     datetime.now().isoformat(),
        "device": device_name,
        "cmd":    cmd,
        "params": params,
        "ok":     result.ok,
        "msg":    result.msg,
    })
    if len(_command_log) > 200:
        del _command_log[:-200]

    return result


def command_all(cmd: str, **params) -> dict[str, CommandResult]:
    """Broadcast a command to all connected devices."""
    results = {}
    for name, device in list(_devices.items()):
        if device.connected:
            results[name] = command(name, cmd, **params)
    return results


# ── Status ─────────────────────────────────────────────────────────────────────

def status() -> str:
    """Human-readable status of all registered devices."""
    if not _devices:
        return "No hardware devices registered."
    lines = [d.status_str() for d in _devices.values()]
    return "\n".join(lines)


def status_dict() -> dict:
    return {
        name: {
            "connected": d.connected,
            "state": d.get_state(),
            "last_seen": d.last_seen.isoformat() if d.last_seen else None,
            "description": d.description,
        }
        for name, d in _devices.items()
    }


def get_log(n: int = 20) -> list[dict]:
    return _command_log[-n:]


# ── Nearby discovery (macOS-native) ───────────────────────────────────────────

_BONJOUR_SERVICE_TYPES: dict[str, str] = {
    "airplay": "_airplay._tcp",
    "raop": "_raop._tcp",
    "companion": "_companion-link._tcp",
    "googlecast": "_googlecast._tcp",
}

_NEARBY_CACHE_LOCK = threading.Lock()
_NEARBY_CACHE_VALUE: dict[str, Any] | None = None
_NEARBY_CACHE_UNTIL = 0.0


def _run_json_command(args: list[str]) -> dict:
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return {}
        return json.loads(proc.stdout)
    except Exception:
        return {}


def discover_bluetooth_devices() -> dict:
    """
    Return Bluetooth controller state plus connected and known nearby devices.
    macOS-only for now, via system_profiler.
    """
    data = _run_json_command(["/usr/sbin/system_profiler", "SPBluetoothDataType", "-json"])
    entries = data.get("SPBluetoothDataType", [])
    if not entries:
        return {"available": False, "controller": {}, "connected": [], "known": []}

    root = entries[0]
    controller = root.get("controller_properties", {}) or {}

    def _flatten(items: list[dict]) -> list[dict]:
        flat: list[dict] = []
        for item in items or []:
            for name, payload in item.items():
                payload = payload or {}
                flat.append({
                    "name": name,
                    "address": payload.get("device_address", ""),
                    "type": payload.get("device_minorType", ""),
                    "rssi": payload.get("device_rssi", ""),
                    "services": payload.get("device_services", ""),
                    "firmware": payload.get("device_firmwareVersion", ""),
                    "vendor_id": payload.get("device_vendorID", ""),
                    "product_id": payload.get("device_productID", ""),
                })
        return flat

    return {
        "available": True,
        "controller": {
            "state": controller.get("controller_state", ""),
            "address": controller.get("controller_address", ""),
            "discoverable": controller.get("controller_discoverable", ""),
            "chipset": controller.get("controller_chipset", ""),
            "services": controller.get("controller_supportedServices", ""),
        },
        "connected": _flatten(root.get("device_connected", [])),
        "known": _flatten(root.get("device_not_connected", [])),
    }


def _browse_bonjour_service(service_type: str, timeout: float = 2.5) -> list[dict]:
    """
    Browse a Bonjour service for a few seconds and return discovered instances.
    Uses dns-sd, which is present on macOS by default.
    """
    try:
        proc = subprocess.Popen(
            ["/usr/bin/dns-sd", "-B", service_type, "local."],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        time.sleep(timeout)
        proc.terminate()
        output, _ = proc.communicate(timeout=2)
    except Exception:
        return []

    instances: dict[tuple[str, str], dict] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or "Instance Name" in line or "...STARTING..." in line or line.startswith("Browsing for ") or line.startswith("DATE:"):
            continue
        parts = line.split()
        if len(parts) < 7:
            continue
        if parts[1] not in {"Add", "Rmv"}:
            continue
        action = parts[1]
        interface = parts[3]
        domain = parts[4]
        instance_name = " ".join(parts[6:])
        key = (instance_name, interface)
        instances[key] = {
            "name": instance_name,
            "interface": interface,
            "domain": domain,
            "service_type": service_type,
            "present": action == "Add",
        }

    return [item for item in instances.values() if item.get("present")]


def discover_network_services(timeout: float = 2.5) -> dict:
    services: dict[str, list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=len(_BONJOUR_SERVICE_TYPES) or 1) as pool:
        futures = {
            pool.submit(_browse_bonjour_service, service_type, timeout=timeout): label
            for label, service_type in _BONJOUR_SERVICE_TYPES.items()
        }
        for future in as_completed(futures):
            label = futures[future]
            try:
                services[label] = future.result()
            except Exception:
                services[label] = []
    return {
        "available": True,
        "services": services,
    }


def discover_nearby(timeout: float = 2.5, force_refresh: bool = False) -> dict:
    """
    Unified nearby-device snapshot for Jarvis.
    Includes Bluetooth plus common local-network services like AirPlay/RAOP.
    """
    global _NEARBY_CACHE_VALUE, _NEARBY_CACHE_UNTIL
    now = time.monotonic()
    with _NEARBY_CACHE_LOCK:
        if not force_refresh and _NEARBY_CACHE_VALUE is not None and now < _NEARBY_CACHE_UNTIL:
            return copy.deepcopy(_NEARBY_CACHE_VALUE)

    bluetooth = discover_bluetooth_devices()
    network = discover_network_services(timeout=timeout)
    snapshot = {
        "ts": datetime.now().isoformat(),
        "bluetooth": bluetooth,
        "network": network,
    }
    with _NEARBY_CACHE_LOCK:
        _NEARBY_CACHE_VALUE = copy.deepcopy(snapshot)
        _NEARBY_CACHE_UNTIL = time.monotonic() + 20.0
    return snapshot


def local_ipv4_addresses() -> list[str]:
    """
    Best-effort list of local IPv4 addresses for same-Wi-Fi handoff.
    Keeps loopback out so the UI shows copyable device-facing addresses.
    """
    addresses: set[str] = set()

    try:
        hostname = socket.gethostname()
        for family, *_rest, sockaddr in socket.getaddrinfo(hostname, None, socket.AF_INET):
            if family == socket.AF_INET:
                ip = sockaddr[0]
                if ip and not ip.startswith("127."):
                    addresses.add(ip)
    except Exception:
        pass

    for iface in ("en0", "en1", "bridge0"):
        try:
            proc = subprocess.run(
                ["/usr/sbin/ipconfig", "getifaddr", iface],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            ip = (proc.stdout or "").strip()
            if ip and not ip.startswith("127."):
                addresses.add(ip)
        except Exception:
            pass

    return sorted(addresses)


def bridge_status(api_host: str = "127.0.0.1", api_port: int = 8765) -> dict:
    """
    Describe whether Jarvis is exposed only locally or to the local network.
    """
    host = (api_host or "127.0.0.1").strip()
    lan_enabled = host in {"0.0.0.0", "::", "*"} or (
        host not in {"127.0.0.1", "localhost", "::1"}
    )
    ips = local_ipv4_addresses()

    if host in {"0.0.0.0", "::", "*"}:
        urls = [f"http://{ip}:{api_port}" for ip in ips]
    elif host in {"127.0.0.1", "localhost", "::1"}:
        urls = [f"http://127.0.0.1:{api_port}"]
    else:
        urls = [f"http://{host}:{api_port}"]

    return {
        "enabled": lan_enabled,
        "host": host,
        "port": int(api_port),
        "local_only": not lan_enabled,
        "ips": ips,
        "urls": urls,
        "primary_url": urls[0] if urls else f"http://127.0.0.1:{api_port}",
    }


_SETTINGS_TARGETS: dict[str, list[str]] = {
    "bluetooth": [
        "x-apple.systempreferences:com.apple.BluetoothSettings",
        "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Bluetooth",
    ],
    "sound": [
        "x-apple.systempreferences:com.apple.Sound-Settings.extension",
        "x-apple.systempreferences:com.apple.preference.sound",
    ],
    "displays": [
        "x-apple.systempreferences:com.apple.Displays-Settings.extension",
        "x-apple.systempreferences:com.apple.preference.displays",
    ],
    "airplay": [
        "x-apple.systempreferences:com.apple.Displays-Settings.extension",
        "x-apple.systempreferences:com.apple.preference.displays",
    ],
}


def open_system_settings(target: str) -> str:
    """
    Open a supported System Settings pane with graceful fallbacks.
    """
    key = (target or "").strip().lower()
    candidates = _SETTINGS_TARGETS.get(key, [])
    for candidate in candidates:
        try:
            proc = subprocess.run(
                ["open", candidate],
                capture_output=True,
                text=True,
                timeout=4,
                check=False,
            )
            if proc.returncode == 0:
                return f"Opened {key} settings."
        except Exception:
            continue

    try:
        proc = subprocess.run(
            ["open", "-a", "System Settings"],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
        if proc.returncode == 0:
            return f"Opened System Settings. Navigate to {key} from there."
    except Exception:
        pass

    return f"Couldn't open {key} settings."


def open_system_settings_result(target: str) -> dict:
    """
    Structured wrapper for API/UI callers.
    """
    message = open_system_settings(target)
    return {
        "ok": message.startswith("Opened "),
        "target": (target or "").strip().lower(),
        "message": message,
    }


# ── Auto-scan serial ports ────────────────────────────────────────────────────

def scan_serial_ports() -> list[str]:
    """Return list of available serial ports (macOS/Linux)."""
    ports = []
    for pattern in ["/dev/tty.usbmodem*", "/dev/tty.usbserial*",
                    "/dev/ttyUSB*", "/dev/ttyACM*"]:
        ports.extend(glob.glob(pattern))
    return sorted(ports)


def auto_connect_serial(name: str, baud: int = 115200,
                        description: str = "") -> SerialDevice | None:
    """
    Auto-detect and connect to the first available USB serial device.
    Useful when you don't know the exact port name.
    """
    ports = scan_serial_ports()
    if not ports:
        print("[HW] No serial ports found.")
        return None
    port = ports[0]
    device = SerialDevice(name, port, baud, description=description)
    register(device)
    return device if device.connected else None


# ── Add hardware commands to the API ──────────────────────────────────────────

def register_api_routes(app):
    """Register hardware endpoints on the FastAPI app."""
    from fastapi import HTTPException
    from pydantic import BaseModel

    class HWCommand(BaseModel):
        device: str
        cmd: str
        params: dict = {}

    @app.get("/hardware/status")
    def hw_status():
        return status_dict()

    @app.post("/hardware/command")
    def hw_command(req: HWCommand):
        result = command(req.device, req.cmd, **req.params)
        return {"ok": result.ok, "msg": result.msg, "state": result.state}

    @app.get("/hardware/devices")
    def hw_devices():
        return [{"name": d.name, "connected": d.connected, "description": d.description}
                for d in list_devices()]

    @app.get("/hardware/log")
    def hw_log(n: int = 20):
        return get_log(n)

    @app.get("/hardware/ports")
    def hw_ports():
        return {"ports": scan_serial_ports()}

    @app.get("/hardware/discover")
    def hw_discover(timeout: float = 2.5):
        return discover_nearby(timeout=timeout)

    @app.get("/hardware/discover/bluetooth")
    def hw_discover_bluetooth():
        return discover_bluetooth_devices()

    @app.get("/hardware/discover/network")
    def hw_discover_network(timeout: float = 2.5):
        return discover_network_services(timeout=timeout)

    @app.post("/hardware/settings/{target}")
    def hw_open_settings(target: str):
        return open_system_settings_result(target)
