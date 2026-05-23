import threading
from contextlib import contextmanager


_AUDIO_CAPTURE_LOCK = threading.RLock()


@contextmanager
def audio_capture(owner: str = "audio"):
    """Serialize native audio input clients inside the Jarvis process."""
    with _AUDIO_CAPTURE_LOCK:
        yield
