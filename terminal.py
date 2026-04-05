import os
import subprocess
import tempfile


_DESTRUCTIVE_PATTERNS = [
    "rm -rf", "rm -fr", "rmdir", "mkfs", "dd if=",
    ":(){:|:&};:",  # fork bomb
    "> /dev/sd", "shred", "wipefs",
    "shutdown", "reboot", "halt", "poweroff",
    "chmod -R 777 /", "chown -R",
]


def _contains_blocked_pattern(command: str) -> str | None:
    lower_cmd = command.lower()
    for pattern in _DESTRUCTIVE_PATTERNS:
        if pattern in lower_cmd:
            return pattern
    return None


def _escape_applescript(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def run_command(command: str, cwd: str = None) -> str:
    """Run a shell command and return its output."""
    pattern = _contains_blocked_pattern(command)
    if pattern:
        return f"Blocked: '{pattern}' is a destructive operation. Be more specific if you really need this."
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=30, cwd=cwd or os.path.expanduser("~")
        )
        output = result.stdout.strip() or result.stderr.strip()
        return output if output else "Command ran with no output."
    except subprocess.TimeoutExpired:
        return "Command timed out after 30 seconds."
    except Exception as e:
        return f"Error running command: {e}"


def run_admin_command(command: str) -> str:
    """
    Run a shell command through macOS's admin prompt.
    The user must approve with their password in the native dialog.
    """
    pattern = _contains_blocked_pattern(command)
    if pattern:
        return f"Blocked: '{pattern}' is a destructive operation. Be more specific if you really need this."

    safe_command = _escape_applescript(command)
    script = f'do shell script "{safe_command}" with administrator privileges'
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=60
        )
        output = result.stdout.strip() or result.stderr.strip()
        if result.returncode != 0:
            return output or "Admin command failed or was cancelled."
        return output if output else "Admin command ran successfully."
    except subprocess.TimeoutExpired:
        return "Admin command timed out waiting for approval or completion."
    except Exception as e:
        return f"Error running admin command: {e}"


def read_file(path: str) -> str:
    """Read a file and return its contents."""
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return f"File not found: {path}"
    try:
        with open(path, "r", errors="replace") as f:
            content = f.read()
        if len(content) > 8000:
            return content[:8000] + f"\n... [truncated — file has {len(content)} chars total]"
        return content
    except Exception as e:
        return f"Could not read file: {e}"


def write_file(path: str, content: str) -> str:
    """Write content to a file."""
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    try:
        with open(path, "w") as f:
            f.write(content)
        return f"File written to {path}."
    except Exception as e:
        return f"Could not write file: {e}"


def list_directory(path: str = "~") -> str:
    """List files in a directory."""
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return f"Directory not found: {path}"
    try:
        entries = os.listdir(path)
        dirs = [e + "/" for e in entries if os.path.isdir(os.path.join(path, e))]
        files = [e for e in entries if os.path.isfile(os.path.join(path, e))]
        result = sorted(dirs) + sorted(files)
        return "\n".join(result[:50]) + (f"\n... and {len(result)-50} more" if len(result) > 50 else "")
    except Exception as e:
        return f"Could not list directory: {e}"


def run_python(code: str) -> str:
    """Execute Python code and return output."""
    path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            f.write(code)
            path = f.name
        result = subprocess.run(
            ["python3", path], capture_output=True, text=True, timeout=15
        )
        output = (result.stdout.strip() or result.stderr.strip())[:4000]
        return output if output else "Code ran with no output."
    except subprocess.TimeoutExpired:
        return "Code timed out."
    except Exception as e:
        return f"Error: {e}"
    finally:
        if path and os.path.exists(path):
            os.unlink(path)


def get_clipboard() -> str:
    """Return current clipboard contents."""
    result = subprocess.run(["pbpaste"], capture_output=True, text=True)
    return result.stdout.strip() or "Clipboard is empty."


def set_clipboard(text: str) -> str:
    """Copy text to clipboard."""
    subprocess.run(["pbcopy"], input=text.encode())
    return "Copied to clipboard."
