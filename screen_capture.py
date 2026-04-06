import os
import subprocess
import tempfile
import time


def _permission_hint(stderr: str) -> str:
    text = (stderr or "").lower()
    if any(
        marker in text
        for marker in (
            "could not create image from display",
            "not permitted",
            "operation not permitted",
            "user canceled",
            "user cancelled",
        )
    ):
        return (
            " Check macOS Screen Recording permission for the app launching Jarvis "
            "in System Settings > Privacy & Security > Screen Recording."
        )
    return ""


def _capture_once(path: str, image_format: str | None = None) -> tuple[bool, str]:
    cmd = ["screencapture", "-x"]
    if image_format:
        cmd.extend(["-t", image_format])
    cmd.append(path)
    result = subprocess.run(cmd, capture_output=True, text=True)
    stderr = (result.stderr or result.stdout or "").strip()
    ok = result.returncode == 0 and os.path.exists(path) and os.path.getsize(path) > 0
    return ok, stderr


def capture_screenshot(
    path: str,
    image_format: str | None = None,
    retries: int = 3,
    delay_s: float = 0.18,
) -> str:
    last_error = ""
    for attempt in range(max(1, retries)):
        ok, stderr = _capture_once(path, image_format=image_format)
        if ok:
            return path
        last_error = stderr or "Unknown screenshot capture failure."
        if os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass
        if attempt < retries - 1:
            time.sleep(delay_s)

    raise RuntimeError(
        "Screenshot capture failed."
        + (f" {last_error}" if last_error else "")
        + _permission_hint(last_error)
    )


def capture_screenshot_temp(
    preferred_format: str = "jpg",
    fallback_formats: tuple[str, ...] = ("png",),
    retries: int = 3,
    delay_s: float = 0.18,
) -> str:
    formats: list[str] = []
    for image_format in (preferred_format, *fallback_formats):
        if image_format and image_format not in formats:
            formats.append(image_format)

    last_error: Exception | None = None
    for image_format in formats:
        fd, path = tempfile.mkstemp(suffix=f".{image_format}")
        os.close(fd)
        try:
            if os.path.exists(path):
                os.unlink(path)
            return capture_screenshot(
                path,
                image_format=image_format,
                retries=retries,
                delay_s=delay_s,
            )
        except Exception as exc:
            last_error = exc
            if os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass

    if last_error:
        raise last_error
    raise RuntimeError("Screenshot capture failed before any capture attempt ran.")
