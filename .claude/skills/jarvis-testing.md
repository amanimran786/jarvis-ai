# Testing Strategy and Patterns

Test narrowly. Prove the change with the smallest meaningful test.

## Philosophy

- Logic/config change → targeted unit test
- UI status regression → targeted regression test
- Packaged-app fix → targeted test + packaged rebuild verification

If you add a regression for a bug, keep it small and directly tied to the real failure mode.

## Common Test Commands

```bash
python3 -m pytest /Users/truthseeker/jarvis-ai/tests/test_voice_tts_regression.py -q
python3 -m pytest /Users/truthseeker/jarvis-ai/tests/test_jarvis_regression_suite.py -k 'VoiceStatusUiRegressionTests or transcript_callback_forwards_to_live_bridge' -q
python3 -m pytest /Users/truthseeker/jarvis-ai/tests/test_unit_coverage.py -q
```

## Mock Injection Pattern

For tests that need to mock PyQt6, sounddevice, or other problematic imports:

```python
import sys
from unittest.mock import MagicMock

# Before importing modules that depend on PyQt6/sounddevice:
sys.modules['PyQt6'] = MagicMock()
sys.modules['PyQt6.QtWidgets'] = MagicMock()
sys.modules['sounddevice'] = MagicMock()

# Now safe to import your code
from your_module import function_under_test
```

This prevents import failures in test environments that don't have those libraries installed or available.

## Key Testing Rules

- Never trust repo runtime behavior as proof of packaged runtime behavior
- For voice tests, verify the actual failing layer (mic → STT → TTS)
- For UI tests, check both rendering and state consistency
- Always verify packaged behavior for code touching voice, UI, or runtime modules

## Test File Organization

Tests live in `/Users/truthseeker/jarvis-ai/tests/`.

Follow existing naming: `test_<domain>_<concern>.py` (e.g., `test_voice_tts_regression.py`).

## Good Test Pattern

1. Set up the minimal environment (mocks, fixtures, state)
2. Execute the function/behavior under test
3. Assert the specific output/side-effect the change is supposed to produce
4. Clean up any state (if needed)

Avoid:

- Testing multiple concerns in one test
- Assertions on unrelated behavior
- Slow/flaky external dependencies

## AAA Test Pattern

Structure every test as Arrange-Act-Assert:

```python
def test_falls_back_to_say_when_kokoro_unavailable():
    # Arrange
    with patch("local_runtime.local_kokoro_tts.speak") as mock_kokoro:
        mock_kokoro.side_effect = RuntimeError("model not loaded")
        # Act
        result = speak_with_fallback("hello")
        # Assert
        assert result is True  # fallback succeeded

```

Name tests to describe exact behavior:
- `test_returns_empty_when_no_match`
- `test_raises_on_missing_config`
- `test_falls_back_to_say_when_kokoro_unavailable`
- `test_voice_status_not_overwritten_by_task_spinner`

