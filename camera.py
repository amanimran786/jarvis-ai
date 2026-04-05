import base64
import tempfile
import os
import cv2
from openai import OpenAI
from config import OPENAI_API_KEY

_client = OpenAI(api_key=OPENAI_API_KEY)


def _capture_frame() -> str:
    """Capture a single frame from the webcam and return path to saved image."""
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
    Capture a webcam frame and ask GPT-4o Vision to describe it.
    prompt: what to ask about the image
    """
    path = None
    try:
        path = _capture_frame()
        with open(path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode("utf-8")

        response = _client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{image_data}"
                            }
                        },
                        {
                            "type": "text",
                            "text": f"You are Jarvis, a helpful AI assistant. {prompt} Be concise — this response will be spoken aloud."
                        }
                    ]
                }
            ],
            max_tokens=300
        )
        return response.choices[0].message.content

    finally:
        if path and os.path.exists(path):
            os.unlink(path)


def screenshot_and_describe(prompt: str = "Describe what's on this screen.") -> str:
    """Take a screenshot and describe it using Vision API."""
    import subprocess
    path = tempfile.mktemp(suffix=".jpg")
    subprocess.run(["screencapture", "-x", "-t", "jpg", path], check=True)
    try:
        with open(path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode("utf-8")

        response = _client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}
                        },
                        {
                            "type": "text",
                            "text": f"You are Jarvis. {prompt} Be concise — this will be spoken aloud."
                        }
                    ]
                }
            ],
            max_tokens=300
        )
        return response.choices[0].message.content
    finally:
        if os.path.exists(path):
            os.unlink(path)
