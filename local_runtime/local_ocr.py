from __future__ import annotations

import logging
import os
import time
from typing import Any

# Configure logger
logger = logging.getLogger("local_ocr")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] [LocalOCR] %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
logger.setLevel(logging.INFO)

_VISION_AVAILABLE = False
_IMPORT_ERROR = ""

try:
    import objc
    from Foundation import NSURL
    from Vision import (
        VNImageRequestHandler,
        VNRecognizeTextRequest,
        VNRequestTextRecognitionLevelAccurate,
        VNRequestTextRecognitionLevelFast,
    )
    _VISION_AVAILABLE = True
except ImportError as e:
    _VISION_AVAILABLE = False
    _IMPORT_ERROR = str(e)
    logger.warning(f"Native macOS Vision OCR is unavailable: {e}")


def _extract_rect(rect: Any) -> tuple[float, float, float, float]:
    """
    Safely extract x, y, width, height from a CGRect or tuple representation.
    """
    try:
        x = getattr(rect, "origin", rect).x
        y = getattr(rect, "origin", rect).y
        w = getattr(rect, "size", rect).width
        h = getattr(rect, "size", rect).height
        return float(x), float(y), float(w), float(h)
    except AttributeError:
        pass

    try:
        if isinstance(rect, (list, tuple)) and len(rect) == 2:
            origin, size = rect
            if isinstance(origin, (list, tuple)) and len(origin) == 2:
                x, y = origin
                w, h = size
                return float(x), float(y), float(w), float(h)
    except (TypeError, ValueError):
        pass

    try:
        if isinstance(rect, (list, tuple)) and len(rect) == 4:
            return float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3])
    except (TypeError, ValueError):
        pass

    if hasattr(rect, "__getitem__"):
        try:
            return float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3])
        except Exception:
            logging.debug("[OCR] silent failure in _extract_rect", exc_info=True)

    raise TypeError(f"Could not parse bounding box geometry of type {type(rect)}: {rect}")


def recognize_text(
    image_path: str,
    recognition_level: str = "accurate",
    uses_language_correction: bool = True,
    recognition_languages: list[str] | None = None,
    allowed_directories: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Perform macOS hardware-accelerated OCR text recognition on an image.
    
    Uses Apple's Vision framework (VNImageRequestHandler & VNRecognizeTextRequest) via PyObjC.
    Converts Apple's bottom-left origin coordinate system to standard top-left origin.
    Memory leaks are prevented by wrapping native calls in a thread-local autorelease pool.
    
    Args:
        image_path: Path to the PNG, JPG, or HEIC image file.
        recognition_level: 'accurate' (slower, precise) or 'fast' (faster, less precise).
        uses_language_correction: Whether to apply linguistic corrections.
        recognition_languages: Optional list of preferred language codes (e.g. ['en-US']).
        allowed_directories: Optional list of directory paths that image_path must reside in.
        
    Returns:
        A list of dicts representing recognized text blocks.
    """
    if not _VISION_AVAILABLE:
        logger.error(f"macOS Vision framework not available. Import error: {_IMPORT_ERROR}")
        return []

    try:
        if not image_path:
            logger.error("OCR requested with an empty image path.")
            return []

        # 1. Path Canonicalization & Protection
        canonical_path = os.path.realpath(image_path)

        # 2. Directory Sandboxing Check
        if allowed_directories:
            resolved_allowed = [os.path.realpath(d) for d in allowed_directories]
            is_safe = False
            for allowed in resolved_allowed:
                # Add trailing path separator to prevent prefix match bypass (e.g. /workspace matching /workspace-secret)
                prefix = os.path.join(allowed, "")
                if canonical_path.startswith(prefix) or canonical_path == allowed:
                    is_safe = True
                    break
            if not is_safe:
                logger.error(f"Security Block: Image path is outside allowed directories: {canonical_path}")
                return []

        # 3. File Extension Whitelisting
        _, ext = os.path.splitext(canonical_path.lower())
        allowed_extensions = {".png", ".jpg", ".jpeg", ".heic", ".tiff", ".bmp"}
        if ext not in allowed_extensions:
            logger.error(f"Unsupported or malicious file extension: '{ext}' in {canonical_path}")
            return []

        # 4. TCC / Permissions Check
        if not os.path.exists(canonical_path):
            # Check parent directory accessibility to diagnose TCC denials vs file-not-found
            parent_dir = os.path.dirname(canonical_path)
            if os.path.exists(parent_dir) and not os.access(parent_dir, os.R_OK):
                logger.error(
                    f"macOS TCC Privilege Block: Read access denied on directory: {parent_dir}. "
                    "Ensure Terminal/iTerm/Jarvis has 'Full Disk Access' or 'Files and Folders' permissions."
                )
            else:
                logger.error(f"Image file not found: {canonical_path}")
            return []

        if not os.path.isfile(canonical_path):
            logger.error(f"Path is not a file: {canonical_path}")
            return []

        if not os.access(canonical_path, os.R_OK):
            logger.error(
                f"Permission denied reading image file: {canonical_path}. "
                "Verify OS file system permissions or macOS TCC constraints."
            )
            return []

        logger.debug(f"Starting native Vision OCR on: {canonical_path}")
        start_time = time.perf_counter()
        parsed_results = []

        # 5. Prevent Memory Leaks via Autorelease Pool
        with objc.autorelease_pool():
            # Convert path to NSURL
            image_url = NSURL.fileURLWithPath_(canonical_path)

            # Initialize request handler
            handler = VNImageRequestHandler.alloc().initWithURL_options_(image_url, None)
            if not handler:
                logger.error(f"Failed to create VNImageRequestHandler for {canonical_path}")
                return []

            # Create VNRecognizeTextRequest
            request = VNRecognizeTextRequest.alloc().init()
            if not request:
                logger.error("Failed to allocate VNRecognizeTextRequest")
                return []

            # Configure request options
            if recognition_level.lower() == "fast":
                level = VNRequestTextRecognitionLevelFast
            else:
                level = VNRequestTextRecognitionLevelAccurate
            request.setRecognitionLevel_(level)
            request.setUsesLanguageCorrection_(uses_language_correction)

            if recognition_languages:
                request.setRecognitionLanguages_(recognition_languages)

            # Perform request synchronously
            success, error = handler.performRequests_error_([request], None)
            if not success:
                error_msg = error.localizedDescription() if error else "Unknown Vision error"
                logger.error(f"Vision text recognition perform failed: {error_msg}")
                return []

            # Retrieve observations
            results = request.results()
            if not results:
                logger.debug(f"Vision OCR completed in {time.perf_counter() - start_time:.4f}s: No text found")
                return []

            for observation in results:
                # Get top candidate
                candidates = observation.topCandidates_(1)
                if not candidates:
                    continue

                top_candidate = candidates[0]
                text = top_candidate.string()
                confidence = top_candidate.confidence()

                # Bounding box coordinates from Apple Vision (bottom-left origin)
                bbox = observation.boundingBox()
                try:
                    x, y, w, h = _extract_rect(bbox)
                except Exception as e:
                    logger.warning(f"Skipping observation bounding box extraction error: {e}")
                    continue

                # Convert Apple bottom-left origin to standard top-left origin screen coordinates
                x_tl = x
                y_tl = 1.0 - y - h
                w_tl = w
                h_tl = h

                # Bound values to [0.0, 1.0] range
                x_tl = max(0.0, min(1.0, x_tl))
                y_tl = max(0.0, min(1.0, y_tl))
                w_tl = max(0.0, min(1.0, w_tl))
                h_tl = max(0.0, min(1.0, h_tl))

                parsed_results.append({
                    "text": str(text),
                    "confidence": float(confidence),
                    "box": {
                        "x": float(round(x_tl, 6)),
                        "y": float(round(y_tl, 6)),
                        "w": float(round(w_tl, 6)),
                        "h": float(round(h_tl, 6))
                    }
                })

        duration = time.perf_counter() - start_time
        logger.info(f"Native Vision OCR recognized {len(parsed_results)} text boxes in {duration:.4f} seconds.")
        return parsed_results

    except Exception as e:
        logger.error(f"Uncaught exception in native Vision OCR: {e}", exc_info=True)
        return []
