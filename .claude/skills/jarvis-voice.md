# Voice/STT/TTS Domain Rules

Voice work in Jarvis requires end-to-end thinking. Bugs often hide at the seams between mic, STT, TTS, and UI.

## Core Principle

Never claim "the mic is broken" or "STT is unavailable" without checking the runtime evidence. Always verify the real failing layer.

## Voice Seams to Check

Bugs in Jarvis come from interactions between:

- Mic permissions and device selection
- PortAudio / `speech_recognition` setup
- Local STT model load (faster-whisper, ONNX assets)
- Packaged assets availability
- TTS timing (Kokoro → `say` fallback)
- Post-speech capture window
- UI status clobbering (generic task status overwriting live voice state)

## Verification Checklist for Voice Work

For any voice issue, verify in order:

1. Did the mic open at all?
2. Which input device was actually used?
3. Was audio captured to the buffer?
4. Did local STT model load and return text (or an error)?
5. Did packaged assets exist (ONNX, VAD models)?
6. Is voice status in UI driven by real voice state, not by unrelated tasks?

## File References

- `voice.py`: voice loop, wake/listen/TTS behavior
- `local_runtime/local_stt.py`: local speech-to-text with faster-whisper
- `local_runtime/local_tts.py`: macOS `say` fallback TTS
- `local_runtime/local_kokoro_tts.py`: Kokoro local TTS path
- `config.py`: STT/TTS configuration defaults

## Runtime Artifacts for Voice Debugging

Check these files in the packaged app:

- `/Users/truthseeker/Library/Application Support/Jarvis/.jarvis_voice.log` — voice loop traces
- `/Users/truthseeker/Library/Application Support/Jarvis/.jarvis_crash.log` — crash info
- `/Users/truthseeker/Library/Application Support/Jarvis/.jarvis_runtime.json` — runtime state

## Common Gotchas

- **PaMacCore AUHAL errors**: Usually permission or device selection. Check system audio preferences and mic permissions in System Settings.
- **STT timeouts**: ONNX model may not be in packaged assets. Verify `/Users/truthseeker/Applications/Jarvis.app/Contents/Resources/` has model files.
- **TTS silence after speech**: Kokoro may fail; `say` fallback should catch it. Check `.jarvis_voice.log` for the actual TTS call.
- **Status UI stuck**: Generic task status (loading spinner) can overwrite live voice status. Voice status must always reflect real mic/STT state, never generic UI state.

## Status Surface Rule

Do not let unrelated task activity (text requests, background tasks) overwrite true voice/runtime status.

If a UI element represents live capability state (mic open, listening, capturing), it must be driven by that capability's real state.
