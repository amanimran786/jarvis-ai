# PyInstaller Packaged App Domain Rules

The packaged Jarvis.app is a real product surface. Source-only verification is not enough.

## Core Principle

Do not assume import success in the repo means bundle success. Always verify the packaged runtime, not just `dist/`.

## When to Test the Packaged App

If your change touches any of these:

- `voice.py`
- `ui.py`
- `main.py`
- `Jarvis.spec`
- anything under `local_runtime/`
- packaged permissions, assets, or app behavior

then treat the packaged app as part of acceptance criteria.

## Build and Install

Use this script to rebuild and test:

```bash
/Users/truthseeker/jarvis-ai/scripts/install_jarvis_app.sh --applications-only
```

Verify the installed bundle, not just `dist/`.

## App Locations

- `/Users/truthseeker/Applications/Jarvis.app` — canonical bundle
- `/Users/truthseeker/Desktop/Jarvis.app` — symlink to Applications bundle

Always verify timestamps and follow symlinks if in doubt.

## Common Packaging Failures

Explicitly watch for these during packaged builds:

- Missing PyInstaller hidden imports (sounddevice, scipy.fftpack, etc.)
- Missing package data files and assets
- Missing ONNX/model/VAD assets in Resources/
- Missing macOS permission plist keys
- Path differences between repo runtime and frozen runtime
- `BrokenPipeError` from print/logging in windowed app mode

If a packaged feature fails, inspect runtime evidence before guessing.

## Runtime Artifacts for Debugging

Check these in the packaged app:

- `/Users/truthseeker/Library/Application Support/Jarvis/.jarvis_crash.log` — crash traces
- `/Users/truthseeker/Library/Application Support/Jarvis/.jarvis_runtime.json` — runtime state
- `/Users/truthseeker/Library/Application Support/Jarvis/.jarvis_voice.log` — voice loop traces

## BrokenPipeError Prevention

Windowed apps cannot write to stdout/stderr after detaching from terminal.

If your change adds print statements or logging to `voice.py`, `ui.py`, or any windowed module:
- Verify no direct `print()` calls in windowed context
- Use logging module instead of print
- Never log to stdout in frozen mode without guards

## Asset Bundling

Check that packaged resources exist:

```bash
ls -la /Users/truthseeker/Applications/Jarvis.app/Contents/Resources/
```

ONNX models, VAD assets, TTS assets, and other data files must be included in `Jarvis.spec` and present in the bundle.

## No Repo Runtime Equivalence

Source-only behavior does not prove packaged behavior. Always verify both.
