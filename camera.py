import base64
import tempfile
import os
import shutil
import subprocess
from functools import lru_cache
from openai import OpenAI
from config import OPENAI_API_KEY
from desktop.screen_capture import capture_screenshot_temp


def _cv2():
    import cv2

    return cv2


@lru_cache(maxsize=1)
def _get_openai_client():
    if not OPENAI_API_KEY:
        return None
    return OpenAI(api_key=OPENAI_API_KEY)


def _cloud_vision_allowed() -> bool:
    try:
        import model_router
        return not model_router.is_open_source_mode()
    except Exception:
        return True


def _run_tesseract(path: str, psm: str, timeout_seconds: float = 12) -> str:
    tesseract = shutil.which("tesseract")
    if not tesseract:
        return ""
    try:
        proc = subprocess.run(
            [tesseract, path, "stdout", "--psm", psm],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        if proc.returncode != 0:
            return ""
        return (proc.stdout or "").strip()
    except Exception:
        return ""


def _score_ocr_text(text: str) -> int:
    if not text:
        return 0
    lines = [line for line in text.splitlines() if line.strip()]
    digits = sum(ch.isdigit() for ch in text)
    letters = sum(ch.isalpha() for ch in text)
    punctuation = sum(ch in ":;=()[]{}./_-<>" for ch in text)
    return len(text) + len(lines) * 24 + digits * 2 + letters + punctuation * 3


def _quick_ocr_probe(path: str, prompt: str) -> dict[str, object]:
    sample = _run_tesseract(path, "6", timeout_seconds=3)
    signal = _ocr_signal(sample)
    visual_prompt = _prompt_prefers_visual(prompt)
    text_prompt = _prompt_prefers_text(prompt)
    prefer_ocr = False

    if signal["chars"] >= 120 and signal["words"] >= 18:
        prefer_ocr = True
    elif signal["lines"] >= 4 and signal["letters"] >= 80:
        prefer_ocr = True
    elif text_prompt and signal["chars"] >= 48:
        prefer_ocr = True
    elif visual_prompt and signal["chars"] < 80:
        prefer_ocr = False

    return {
        "sample_text": sample,
        "prefer_ocr": prefer_ocr,
        "signal": signal,
    }


def _ocr_signal(text: str) -> dict[str, int]:
    lines = [line for line in text.splitlines() if line.strip()]
    words = [word for word in text.split() if word.strip()]
    letters = sum(ch.isalpha() for ch in text)
    digits = sum(ch.isdigit() for ch in text)
    return {
        "chars": len(text.strip()),
        "lines": len(lines),
        "words": len(words),
        "letters": letters,
        "digits": digits,
    }


def _prompt_prefers_text(prompt: str) -> bool:
    lower = (prompt or "").lower()
    triggers = (
        "read",
        "ocr",
        "text",
        "code",
        "email",
        "article",
        "document",
        "website",
        "page",
        "screen",
        "screenshot",
        "what does this say",
        "summarize",
    )
    return any(token in lower for token in triggers)


def _prompt_prefers_visual(prompt: str) -> bool:
    lower = (prompt or "").lower()
    triggers = (
        "chart",
        "graph",
        "diagram",
        "layout",
        "photo",
        "picture",
        "person",
        "object",
        "color",
        "scene",
        "where",
        "left",
        "right",
    )
    return any(token in lower for token in triggers)


def _should_prefer_ocr_for_screenshot(prompt: str, ocr_text: str) -> bool:
    signal = _ocr_signal(ocr_text)
    if signal["chars"] >= 120 and signal["words"] >= 18:
        return True
    if signal["lines"] >= 4 and signal["letters"] >= 80:
        return True
    return _prompt_prefers_text(prompt) and signal["chars"] >= 48


def _preprocess_for_ocr(path: str) -> str:
    tmp_path = None
    try:
        cv2 = _cv2()
        image = cv2.imread(path)
        if image is None:
            return ""
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        upscaled = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
        normalized = cv2.GaussianBlur(upscaled, (3, 3), 0)
        processed = cv2.adaptiveThreshold(
            normalized,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            31,
            11,
        )
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.close()
        cv2.imwrite(tmp.name, processed)
        tmp_path = tmp.name
        return tmp_path
    except Exception:
        return ""
    finally:
        # ownership of tmp_path passes to caller on success
        pass


def _extract_ocr_text(path: str) -> str:
    candidates = []
    original_passes = ("6", "11")
    for psm in original_passes:
        text = _run_tesseract(path, psm)
        if text:
            candidates.append(text)

    processed_path = _preprocess_for_ocr(path)
    try:
        if processed_path:
            for psm in ("6", "11", "12"):
                text = _run_tesseract(processed_path, psm)
                if text:
                    candidates.append(text)
    finally:
        if processed_path and os.path.exists(processed_path):
            os.unlink(processed_path)

    if not candidates:
        return ""
    return max(candidates, key=_score_ocr_text)


def _local_vision_summary(prompt: str, ocr_text: str) -> str:
    if not ocr_text.strip():
        return ""
    from brains.brain_ollama import ask_local_stream

    summary_prompt = (
        f"{prompt}\n\n"
        "Use only the OCR evidence below. If OCR is partial, say so briefly and summarize only what is supported.\n\n"
        f"OCR:\n{ocr_text[:8000]}"
    )
    return "".join(
        ask_local_stream(
            summary_prompt,
            system_extra="Use only the supplied OCR evidence. Do not invent unseen visual details. Keep the answer concise and practical.",
            raise_on_error=False,
        )
    ).strip()


def _local_vision_unavailable_message() -> str:
    try:
        from brains import brain_ollama
        caps = brain_ollama.local_capabilities()
        candidates = caps.get("vision_candidates") or []
        health = caps.get("vision_health") or {}
        if candidates:
            unstable = [model for model in candidates if model in health]
            if unstable:
                return (
                    "Local vision is installed but currently unstable. "
                    f"Jarvis quarantined {unstable[0]} for a short cooldown. "
                    "Try again in a moment or pull an alternate model like: ollama pull minicpm-v"
                )
            return (
                "Local vision is installed, but it did not return a usable answer for this image. "
                "Try again or pull an alternate model like: ollama pull minicpm-v"
            )
    except Exception:
        pass
    return "No local vision model is available. Pull one with: ollama pull llava:7b"


def _capture_frame() -> str:
    """Capture a single frame from the webcam and return path to saved image."""
    cv2 = _cv2()
    cam = cv2.VideoCapture(0)
    if not cam.isOpened():
        raise RuntimeError("Could not access the webcam.")

    # Let the camera warm up for a couple frames
    for _ in range(3):
        cam.read()

    ret, frame = cam.read()
    cam.release()

    if not ret:
        raise RuntimeError("Failed to capture frame from webcam.")

    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    cv2.imwrite(tmp.name, frame)
    return tmp.name


def see(prompt: str = "Describe what you see in detail.") -> str:
    """
    Capture a webcam frame and describe it — fully local when possible.

    Priority:
      1. Local multimodal model (llava/minicpm-v) — actual image understanding
      2. OCR + local LLM summary — text-heavy screens
      3. GPT-4o Vision — paid fallback only outside open-source mode
    """
    path = None
    try:
        path = _capture_frame()
        from brains.brain_ollama import ask_local_vision
        local_vision = ask_local_vision(
            path, prompt,
            system_extra="Be concise. This response will be spoken aloud. No markdown."
        )
        if local_vision:
            return local_vision
        local_answer = _local_vision_summary(prompt, _extract_ocr_text(path))
        if local_answer:
            return local_answer
        if not _cloud_vision_allowed():
            return "Open-source mode is active. " + _local_vision_unavailable_message()
        client = _get_openai_client()
        if client is None:
            return _local_vision_unavailable_message()
        with open(path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode("utf-8")
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}},
                {"type": "text", "text": f"You are Jarvis. {prompt} Be concise — spoken aloud."}
            ]}],
            max_tokens=300,
        )
        return response.choices[0].message.content
    finally:
        if path and os.path.exists(path):
            os.unlink(path)


def screenshot_and_describe(prompt: str = "Describe what's on this screen.") -> str:
    """Take a screenshot and describe it — fully local when possible.

    Priority:
      1. Local multimodal model (llava/minicpm-v)
      2. OCR + local LLM summary
      3. GPT-4o Vision — paid fallback only outside open-source mode
    """
    path = capture_screenshot_temp(preferred_format="png", fallback_formats=("jpg",))
    try:
        probe = _quick_ocr_probe(path, prompt)
        ocr_text = ""
        prefer_ocr = bool(probe.get("prefer_ocr"))
        if prefer_ocr:
            ocr_text = _extract_ocr_text(path)
            prefer_ocr = _should_prefer_ocr_for_screenshot(prompt, ocr_text or str(probe.get("sample_text") or ""))
            local_answer = _local_vision_summary(prompt, ocr_text)
            if local_answer:
                return local_answer
        from brains.brain_ollama import ask_local_vision
        local_vision = ask_local_vision(
            path, prompt,
            system_extra="Be concise. This response will be spoken aloud. No markdown."
        )
        if local_vision:
            return local_vision
        if not prefer_ocr:
            ocr_text = ocr_text or _extract_ocr_text(path)
            local_answer = _local_vision_summary(prompt, ocr_text)
            if local_answer:
                return local_answer
        if not _cloud_vision_allowed():
            return "Open-source mode is active. " + _local_vision_unavailable_message()
        client = _get_openai_client()
        if client is None:
            return _local_vision_unavailable_message()
        with open(path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode("utf-8")
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}},
                {"type": "text", "text": f"You are Jarvis. {prompt}"},
            ]}],
            max_tokens=1024,
        )
        return response.choices[0].message.content
    finally:
        if os.path.exists(path):
            os.unlink(path)
