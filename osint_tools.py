from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")

_CACHE_TTL: dict[str, int] = {"maigret": 3600, "dnstwist": 1800}


class _ScanCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        db_path = Path.home() / ".jarvis" / "osint_cache.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS scans "
            "(id TEXT PRIMARY KEY, tool TEXT NOT NULL, target TEXT NOT NULL, "
            "result_json TEXT NOT NULL, created_at REAL NOT NULL)"
        )
        self._conn.commit()

    def get(self, tool: str, target: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT result_json, created_at FROM scans WHERE id = ?",
                (f"{tool}:{target}",),
            ).fetchone()
            if not row:
                return None
            result_json, created_at = row
            ttl = _CACHE_TTL.get(tool, 3600)
            if time.time() - created_at > ttl:
                self._conn.execute("DELETE FROM scans WHERE id = ?", (f"{tool}:{target}",))
                self._conn.commit()
                return None
            return json.loads(result_json)

    def put(self, tool: str, target: str, result: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO scans (id, tool, target, result_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"{tool}:{target}", tool, target, json.dumps(result), time.time()),
            )
            self._conn.commit()

    def clear(self, tool: str | None = None, target: str | None = None) -> None:
        with self._lock:
            if tool and target:
                self._conn.execute("DELETE FROM scans WHERE id = ?", (f"{tool}:{target}",))
            elif tool:
                self._conn.execute("DELETE FROM scans WHERE tool = ?", (tool,))
            elif target:
                self._conn.execute("DELETE FROM scans WHERE target = ?", (target,))
            else:
                self._conn.execute("DELETE FROM scans")
            self._conn.commit()

    def list_entries(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT tool, target, created_at FROM scans ORDER BY created_at DESC"
            ).fetchall()
            return [{"tool": r[0], "target": r[1], "cached_at": r[2]} for r in rows]


_cache = _ScanCache()


def status() -> dict:
    maigret_cmd = shutil.which("maigret")
    dnstwist_cmd = shutil.which("dnstwist")
    subfinder_cmd = shutil.which("subfinder")
    whois_cmd = shutil.which("whois")
    return {
        "maigret": {"available": bool(maigret_cmd), "command": maigret_cmd or ""},
        "dnstwist": {"available": bool(dnstwist_cmd), "command": dnstwist_cmd or ""},
        "subfinder": {"available": bool(subfinder_cmd), "command": subfinder_cmd or ""},
        "whois": {"available": bool(whois_cmd), "command": whois_cmd or ""},
    }


def _normalize_username(value: str) -> str:
    username = (value or "").strip()
    return username if _USERNAME_RE.fullmatch(username) else ""


def _normalize_domain(value: str) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    if "://" in raw:
        parsed = urlparse(raw)
        host = (parsed.hostname or "").strip().lower()
    else:
        host = raw.split("/", 1)[0].split(":", 1)[0].strip()
    if host.startswith("www."):
        host = host[4:]
    return host if _DOMAIN_RE.fullmatch(host) else ""


def _run_command(argv: list[str], timeout_seconds: int) -> tuple[int, str, str]:
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=max(5, min(300, int(timeout_seconds))),
        check=False,
    )
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def _try_parse_json(text: str) -> dict | list | None:
    blob = (text or "").strip()
    if not blob:
        return None
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        return None


def _extract_maigret_profiles(payload: dict | list, username: str, max_results: int) -> list[dict]:
    rows: list[dict] = []

    def maybe_add(site_name: str, info: dict):
        status = str(info.get("status", "")).strip()
        status_l = status.lower()
        url = (
            str(info.get("url_user") or info.get("profile_url") or info.get("url") or "").strip()
        )
        found = False
        if isinstance(info.get("claimed"), bool):
            found = bool(info["claimed"])
        elif isinstance(info.get("exists"), bool):
            found = bool(info["exists"])
        elif status_l:
            found = any(token in status_l for token in ("claimed", "found", "exists"))
            if any(token in status_l for token in ("available", "not found", "unknown", "unchecked", "illegal")):
                found = False
        if not found:
            return
        if not url:
            url = f"https://{site_name}/{username}"
        rows.append(
            {
                "site": site_name,
                "url": url,
                "status": status or ("found" if found else ""),
            }
        )

    if isinstance(payload, dict):
        sites = payload.get("sites")
        if isinstance(sites, dict):
            for site_name, info in sites.items():
                if isinstance(info, dict):
                    maybe_add(str(site_name), info)
        elif isinstance(payload.get("results"), list):
            for item in payload.get("results", []):
                if not isinstance(item, dict):
                    continue
                site = str(item.get("site") or item.get("name") or "").strip()
                if site:
                    maybe_add(site, item)
    elif isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            site = str(item.get("site") or item.get("name") or "").strip()
            if site:
                maybe_add(site, item)

    rows.sort(key=lambda item: (item["site"].lower(), item["url"].lower()))
    return rows[: max(1, min(200, int(max_results)))]


def username_lookup(
    username: str,
    timeout_seconds: int = 45,
    top_sites: int = 200,
    max_results: int = 25,
) -> dict:
    normalized = _normalize_username(username)
    if not normalized:
        return {
            "ok": False,
            "error": "invalid_username",
            "message": "Username must match [A-Za-z0-9._-] and be 1-64 chars.",
        }

    cached = _cache.get("maigret", normalized)
    if cached:
        return {
            "ok": True,
            "provider": "maigret",
            "username": normalized,
            "profiles": cached["profiles"],
            "found_count": len(cached["profiles"]),
            "cache_hit": True,
        }

    cmd = shutil.which("maigret")
    if not cmd:
        return {
            "ok": False,
            "error": "tool_missing",
            "message": "maigret is not installed. Install with: pip install maigret",
        }

    safe_timeout = max(10, min(300, int(timeout_seconds)))
    safe_top_sites = max(20, min(500, int(top_sites)))
    payload: dict | list | None = None
    stderr_messages: list[str] = []
    command_used: list[str] = []

    with tempfile.TemporaryDirectory(prefix="jarvis-maigret-") as tmpdir:
        output_path = Path(tmpdir) / "maigret.json"
        attempts = [
            [
                cmd,
                normalized,
                "--json",
                str(output_path),
                "--top-sites",
                str(safe_top_sites),
                "--no-color",
            ],
            [cmd, normalized, "--json", str(output_path)],
            [cmd, normalized, "--json"],
        ]
        for argv in attempts:
            try:
                code, stdout, stderr = _run_command(argv, safe_timeout)
            except subprocess.TimeoutExpired:
                stderr_messages.append("timeout")
                continue
            if stderr:
                stderr_messages.append(stderr)
            if output_path.exists():
                try:
                    payload = json.loads(output_path.read_text(encoding="utf-8"))
                    command_used = argv
                    break
                except Exception:
                    payload = None
            parsed = _try_parse_json(stdout)
            if parsed is not None:
                payload = parsed
                command_used = argv
                break
            if code == 0 and stdout:
                parsed = _try_parse_json(stdout.splitlines()[-1])
                if parsed is not None:
                    payload = parsed
                    command_used = argv
                    break

    if payload is None:
        detail = " | ".join(msg for msg in stderr_messages if msg)[:800]
        return {
            "ok": False,
            "error": "scan_failed",
            "message": f"Failed to run maigret for {normalized}.",
            "detail": detail,
        }

    profiles = _extract_maigret_profiles(payload, normalized, max_results=max_results)
    _cache.put("maigret", normalized, {"profiles": profiles})
    return {
        "ok": True,
        "provider": "maigret",
        "username": normalized,
        "profiles": profiles,
        "found_count": len(profiles),
        "cache_hit": False,
        "command": " ".join(command_used) if command_used else cmd,
    }


def _extract_dnstwist_candidates(payload: dict | list, domain: str, max_results: int, registered_only: bool) -> list[dict]:
    if isinstance(payload, dict):
        candidates = payload.get("domains") if isinstance(payload.get("domains"), list) else []
    elif isinstance(payload, list):
        candidates = payload
    else:
        candidates = []

    rows: list[dict] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        candidate = str(item.get("domain") or item.get("domain-name") or item.get("fqdn") or "").strip().lower()
        if not candidate or candidate == domain:
            continue
        fuzzer = str(item.get("fuzzer") or "").strip()
        if fuzzer.lower() in {"*", "original", "original*"}:
            continue
        dns_a = item.get("dns_a", item.get("dns-a"))
        dns_aaaa = item.get("dns_aaaa", item.get("dns-aaaa"))
        mx = item.get("mx")
        ns = item.get("ns")
        whois_created = item.get("whois_created", item.get("whois-created"))
        has_dns = bool(dns_a or dns_aaaa or mx or ns)
        has_whois = bool(whois_created)
        if registered_only and not (has_dns or has_whois):
            continue

        risk_score = 0
        if has_dns:
            risk_score += 2
        if has_whois:
            risk_score += 2
        if mx:
            risk_score += 1

        rows.append(
            {
                "domain": candidate,
                "fuzzer": fuzzer,
                "dns_a": dns_a or [],
                "dns_aaaa": dns_aaaa or [],
                "mx": mx or [],
                "ns": ns or [],
                "whois_created": whois_created or "",
                "risk_score": risk_score,
            }
        )

    rows.sort(key=lambda row: (-int(row.get("risk_score", 0)), row.get("domain", "")))
    return rows[: max(1, min(500, int(max_results)))]


def domain_typo_scan(
    domain: str,
    timeout_seconds: int = 60,
    max_results: int = 25,
    registered_only: bool = True,
) -> dict:
    normalized = _normalize_domain(domain)
    if not normalized:
        return {
            "ok": False,
            "error": "invalid_domain",
            "message": "Domain must be a valid hostname, e.g. example.com.",
        }

    cached = _cache.get("dnstwist", normalized)
    if cached:
        return {
            "ok": True,
            "provider": "dnstwist",
            "domain": normalized,
            "registered_only": bool(registered_only),
            "candidates": cached["candidates"],
            "candidate_count": len(cached["candidates"]),
            "cache_hit": True,
        }

    cmd = shutil.which("dnstwist")
    if not cmd:
        return {
            "ok": False,
            "error": "tool_missing",
            "message": "dnstwist is not installed. Install with: pip install dnstwist",
        }

    safe_timeout = max(10, min(300, int(timeout_seconds)))
    payload: dict | list | None = None
    stderr_messages: list[str] = []
    command_used: list[str] = []

    attempts: list[list[str]] = []
    if registered_only:
        attempts.append([cmd, "--registered", "--format", "json", normalized])
    attempts.extend(
        [
            [cmd, "--format", "json", normalized],
            [cmd, "--json", normalized],
        ]
    )

    for argv in attempts:
        try:
            _code, stdout, stderr = _run_command(argv, safe_timeout)
        except subprocess.TimeoutExpired:
            stderr_messages.append("timeout")
            continue
        if stderr:
            stderr_messages.append(stderr)
        parsed = _try_parse_json(stdout)
        if parsed is not None:
            payload = parsed
            command_used = argv
            break

    if payload is None:
        detail = " | ".join(msg for msg in stderr_messages if msg)[:800]
        return {
            "ok": False,
            "error": "scan_failed",
            "message": f"Failed to run dnstwist for {normalized}.",
            "detail": detail,
        }

    candidates = _extract_dnstwist_candidates(
        payload,
        domain=normalized,
        max_results=max_results,
        registered_only=registered_only,
    )
    _cache.put("dnstwist", normalized, {"candidates": candidates})
    return {
        "ok": True,
        "provider": "dnstwist",
        "domain": normalized,
        "registered_only": bool(registered_only),
        "candidates": candidates,
        "candidate_count": len(candidates),
        "cache_hit": False,
        "command": " ".join(command_used) if command_used else cmd,
    }


def subdomain_enum(
    domain: str,
    timeout_seconds: int = 60,
    max_results: int = 100,
    passive_only: bool = True,
) -> dict:
    normalized = _normalize_domain(domain)
    if not normalized:
        return {
            "ok": False,
            "error": "invalid_domain",
            "message": "Domain must be a valid hostname, e.g. example.com.",
        }

    cmd = shutil.which("subfinder")
    if not cmd:
        return {
            "ok": False,
            "error": "tool_missing",
            "message": "subfinder is not installed. Install with: go install github.com/projectdiscovery/subfinder/v2/cmd/subfinder@latest",
        }

    safe_timeout = max(10, min(300, int(timeout_seconds)))
    per_source_timeout = max(5, safe_timeout // 3)
    argv = [cmd, "-d", normalized, "-silent", "-json", "-timeout", str(per_source_timeout)]
    if not passive_only:
        argv.append("-active")
    try:
        code, stdout, stderr = _run_command(argv, safe_timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "message": f"subfinder timed out for {normalized}"}

    if code != 0 and not stdout.strip():
        return {
            "ok": False,
            "error": "scan_failed",
            "message": f"subfinder failed for {normalized}",
            "detail": stderr[:400],
        }

    subdomains: list[dict] = []
    for line in stdout.strip().splitlines():
        try:
            info = json.loads(line)
            subdomains.append({
                "host": info.get("host") or info.get("subdomain") or str(info),
                "source": info.get("source", ""),
                "ip": info.get("ip", ""),
            })
        except json.JSONDecodeError:
            if line.strip():
                subdomains.append({"host": line.strip(), "source": "", "ip": ""})

    return {
        "ok": True,
        "provider": "subfinder",
        "domain": normalized,
        "subdomains": subdomains[: max(1, min(1000, int(max_results)))],
        "count": len(subdomains),
        "command": " ".join(argv),
    }


def whois_lookup(domain: str, timeout_seconds: int = 15) -> dict:
    normalized = _normalize_domain(domain)
    if not normalized:
        return {
            "ok": False,
            "error": "invalid_domain",
            "message": "Domain must be a valid hostname, e.g. example.com.",
        }

    cmd = shutil.which("whois")
    if not cmd:
        return {
            "ok": False,
            "error": "tool_missing",
            "message": "whois is not installed. Install with: brew install whois",
        }

    safe_timeout = max(5, min(60, int(timeout_seconds)))
    try:
        code, stdout, stderr = _run_command([cmd, normalized], safe_timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "message": f"whois timed out for {normalized}"}

    if not stdout and code != 0:
        return {
            "ok": False,
            "error": "lookup_failed",
            "message": f"whois failed for {normalized}",
            "detail": stderr[:400],
        }

    def _extract(pattern: str) -> str | None:
        m = re.search(pattern, stdout, re.IGNORECASE)
        return m.group(1).strip() if m else None

    def _extract_all(pattern: str) -> list[str]:
        return [m.strip() for m in re.findall(pattern, stdout, re.IGNORECASE)]

    return {
        "ok": True,
        "provider": "whois",
        "domain": normalized,
        "registrar": _extract(r"Registrar:\s*(.+)"),
        "created": _extract(r"Creation Date:\s*(.+)"),
        "expires": _extract(r"(?:Expir(?:y|ation|es) Date|Registry Expiry Date):\s*(.+)"),
        "name_servers": _extract_all(r"Name Server:\s*(.+)"),
        "status": _extract_all(r"Domain Status:\s*(.+?)(?:\s+https?://|$)"),
        "registrant_org": _extract(r"Registrant Organization:\s*(.+)"),
        "raw_truncated": stdout[:800],
    }


def clear_cache(tool: str | None = None, target: str | None = None) -> dict:
    _cache.clear(tool=tool, target=target)
    return {"ok": True}


def list_cache() -> dict:
    return {"ok": True, "entries": _cache.list_entries()}
