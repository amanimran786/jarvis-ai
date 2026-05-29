import sys
import os
import unittest
import tempfile
from unittest.mock import MagicMock, patch

# Insert project root into sys.path to ensure local_runtime is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from local_runtime.local_ocr import _extract_rect, recognize_text, _VISION_AVAILABLE

class TestLocalOCRGeometry(unittest.TestCase):
    def test_extract_rect_cgrect_attributes(self):
        """Test CGRect style attributes matching PyObjC wrapper structure."""
        class MockOrigin:
            def __init__(self, x, y):
                self.x = x
                self.y = y

        class MockSize:
            def __init__(self, width, height):
                self.width = width
                self.height = height

        class MockCGRect:
            def __init__(self, x, y, w, h):
                self.origin = MockOrigin(x, y)
                self.size = MockSize(w, h)

        rect = MockCGRect(10.0, 20.0, 30.0, 40.0)
        self.assertEqual(_extract_rect(rect), (10.0, 20.0, 30.0, 40.0))

    def test_extract_rect_nested_tuples(self):
        """Test CGRect represented as nested tuples: ((x, y), (w, h))."""
        rect = ((10.0, 20.0), (30.0, 40.0))
        self.assertEqual(_extract_rect(rect), (10.0, 20.0, 30.0, 40.0))

        # Nested list/tuple mix
        rect_mix = ([15.5, 25.5], [35.5, 45.5])
        self.assertEqual(_extract_rect(rect_mix), (15.5, 25.5, 35.5, 45.5))

    def test_extract_rect_flat_list_or_tuple(self):
        """Test CGRect represented as a flat 4-tuple or list: (x, y, w, h)."""
        rect_tuple = (10.0, 20.0, 30.0, 40.0)
        self.assertEqual(_extract_rect(rect_tuple), (10.0, 20.0, 30.0, 40.0))

        rect_list = [15.5, 25.5, 35.5, 45.5]
        self.assertEqual(_extract_rect(rect_list), (15.5, 25.5, 35.5, 45.5))

    def test_extract_rect_index_fallback(self):
        """Test CGRect using index-based fallback for objects implementing __getitem__."""
        class IndexableObject:
            def __init__(self, coords):
                self.coords = coords
            def __getitem__(self, idx):
                return self.coords[idx]

        obj = IndexableObject([10.0, 20.0, 30.0, 40.0])
        self.assertEqual(_extract_rect(obj), (10.0, 20.0, 30.0, 40.0))

    def test_extract_rect_invalid_types(self):
        """Test that invalid types raise TypeError."""
        with self.assertRaises(TypeError):
            _extract_rect("not a rect")
        with self.assertRaises(TypeError):
            _extract_rect((1.0, 2.0))  # too short
        with self.assertRaises(TypeError):
            _extract_rect(None)


class TestLocalOCRFlow(unittest.TestCase):
    def test_import_and_availability_status(self):
        """Verify import succeeded and availability is a boolean."""
        self.assertIsInstance(_VISION_AVAILABLE, bool)

    @patch("local_runtime.local_ocr._VISION_AVAILABLE", True)
    @patch("local_runtime.local_ocr.NSURL")
    @patch("local_runtime.local_ocr.VNImageRequestHandler")
    @patch("local_runtime.local_ocr.VNRecognizeTextRequest")
    def test_recognize_text_coordinate_conversion(self, mock_request_cls, mock_handler_cls, mock_nsurl_cls):
        """Verify bottom-left Apple coordinate conversion to top-left coordinate system."""
        # 1. Setup mock URL
        mock_url = MagicMock()
        mock_nsurl_cls.fileURLWithPath_.return_value = mock_url

        # 2. Setup mock request
        mock_request = MagicMock()
        mock_request_cls.alloc().init.return_value = mock_request

        # 3. Setup mock handler
        mock_handler = MagicMock()
        mock_handler_cls.alloc().initWithURL_options_.return_value = mock_handler
        mock_handler.performRequests_error_.return_value = (True, None)

        # 4. Mock observations
        # PyObjC attributes geometry mapping
        class MockBBox:
            def __init__(self, x, y, w, h):
                self.x = x
                self.y = y
                self.width = w
                self.height = h

        # Apple Vision bottom-left coordinates:
        # BBox 1: bottom-left (x=0.1, y=0.2), w=0.3, h=0.4
        # Expected top-left coordinates:
        # x_tl = 0.1, y_tl = 1.0 - 0.2 - 0.4 = 0.4
        mock_observation1 = MagicMock()
        mock_observation1.boundingBox.return_value = MockBBox(0.1, 0.2, 0.3, 0.4)
        
        mock_candidate1 = MagicMock()
        mock_candidate1.string.return_value = "Line One"
        mock_candidate1.confidence.return_value = 0.95
        mock_observation1.topCandidates_.return_value = [mock_candidate1]

        # BBox 2: bottom-left (x=0.5, y=0.6), w=0.2, h=0.2
        # Expected top-left coordinates:
        # x_tl = 0.5, y_tl = 1.0 - 0.6 - 0.2 = 0.2
        mock_observation2 = MagicMock()
        mock_observation2.boundingBox.return_value = MockBBox(0.5, 0.6, 0.2, 0.2)

        mock_candidate2 = MagicMock()
        mock_candidate2.string.return_value = "Line Two"
        mock_candidate2.confidence.return_value = 0.88
        mock_observation2.topCandidates_.return_value = [mock_candidate2]

        mock_request.results.return_value = [mock_observation1, mock_observation2]

        # Create a dummy image file
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(b"fake image data")
            temp_path = tmp.name

        try:
            results = recognize_text(temp_path)
            self.assertEqual(len(results), 2)

            # Assert first recognized block coordinates
            self.assertEqual(results[0]["text"], "Line One")
            self.assertAlmostEqual(results[0]["confidence"], 0.95)
            self.assertAlmostEqual(results[0]["box"]["x"], 0.1)
            self.assertAlmostEqual(results[0]["box"]["y"], 0.4)
            self.assertAlmostEqual(results[0]["box"]["w"], 0.3)
            self.assertAlmostEqual(results[0]["box"]["h"], 0.4)

            # Assert second recognized block coordinates
            self.assertEqual(results[1]["text"], "Line Two")
            self.assertAlmostEqual(results[1]["confidence"], 0.88)
            self.assertAlmostEqual(results[1]["box"]["x"], 0.5)
            self.assertAlmostEqual(results[1]["box"]["y"], 0.2)
            self.assertAlmostEqual(results[1]["box"]["w"], 0.2)
            self.assertAlmostEqual(results[1]["box"]["h"], 0.2)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def test_recognize_text_missing_file(self):
        """Verify recognize_text returns empty list gracefully for missing file."""
        results = recognize_text("/nonexistent/file/path/image.png")
        self.assertEqual(results, [])

    def test_recognize_text_when_vision_unavailable(self):
        """Verify recognize_text returns empty list when Vision is not available."""
        with patch("local_runtime.local_ocr._VISION_AVAILABLE", False):
            # Create a dummy image file
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp.write(b"fake image data")
                temp_path = tmp.name

            try:
                results = recognize_text(temp_path)
                self.assertEqual(results, [])
            finally:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)

    def test_real_run_if_supported(self):
        """Perform a real test run on a tiny temp file if Vision is available."""
        # Create a tiny 1x1 PNG or just a simple temp file
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(b"fake image data")
            temp_path = tmp.name

        try:
            results = recognize_text(temp_path)
            self.assertIsInstance(results, list)
            # Since the image is dummy/fake or has no text, it should return either a list of results
            # (if Vision is available and handles the file) or [] gracefully.
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
