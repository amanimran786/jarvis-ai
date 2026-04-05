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
import threading
import time
import glob
import logging
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
