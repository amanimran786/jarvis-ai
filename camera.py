import base64
import tempfile
import os
import shutil
import subprocess
from functools import lru_cache
from openai import OpenAI
from config import OPENAI_API_KEY
from screen_capture import capture_screenshot_temp


def _cv2():
    import cv2

    return cv2


@lru_cache(maxsize=1)
def _get_openai_client():
    if not OPENAI_API_KEY:
        return None
    return OpenAI(api_key=OPENAI_API_KEY)


def _run_tesseract(path: str, psm: str) -> str:
    tesseract = shutil.which("tesseract")
    if not tesseract:
        return ""
    try:
        proc = subprocess.run(
            [tesseract, path, "stdout", "--psm", psm],
            capture_output=True,
            text=True,
            timeout=12,
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
    from brain_ollama import ask_local_stream

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
      3. GPT-4o Vision — cloud fallback only when no local vision model is available
    """
    path = None
    try:
        path = _capture_frame()
        from brain_ollama import ask_local_vision
        local_vision = ask_local_vision(
            path, prompt,
            system_extra="Be concise. This response will be spoken aloud. No markdown."
        )
        if local_vision:
            return local_vision
        local_answer = _local_vision_summary(prompt, _extract_ocr_text(path))
        if local_answer:
            return local_answer
        client = _get_openai_client()
        if client is None:
            return "No local vision model is available. Pull one with: ollama pull llava:7b"
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
      3. GPT-4o Vision — cloud fallback only
    """
    path = capture_screenshot_temp(preferred_format="jpg", fallback_formats=("png",))
    try:
        from brain_ollama import ask_local_vision
        local_vision = ask_local_vision(
            path, prompt,
            system_extra="Be concise. This response will be spoken aloud. No markdown."
        )
        if local_vision:
            return local_vision
        local_answer = _local_vision_summary(prompt, _extract_ocr_text(path))
        if local_answer:
            return local_answer
        client = _get_openai_client()
        if client is None:
            return "No local vision model is available. Pull one with: ollama pull llava:7b"
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
