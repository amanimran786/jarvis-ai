# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import shutil

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules


ROOT = Path.cwd().resolve()
ICON = ROOT / "assets" / "jarvis.icns"
EXCLUDE_DIRS = {
    ".git",
    ".idea",
    ".claude",
    "__pycache__",
    "venv",
    "build",
    "dist",
    "docs",
    "tests",
    "graphify-out",
    "memory",
    "training",
    ".jarvis_backups",
}
EXCLUDE_EXTS = {
    ".py",
    ".pyc",
    ".pyo",
    ".spec",
}


def iter_datas():
    datas = []
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if not path.is_file():
            continue
        if path.suffix in EXCLUDE_EXTS:
            continue
        if rel.parts[:2] in {("vault", "indexes"), ("vault", "outputs")}:
            continue
        datas.append((str(path), str(path.parent.relative_to(ROOT))))
    return datas


def ensure_collect_destination_dirs():
    """Work around PyInstaller COLLECT missing some nested Qt plugin directories."""
    base = ROOT / "dist" / "Jarvis" / "_internal"
    for _, dest in collect_dynamic_libs("PyQt6"):
        if dest:
            (base / dest).mkdir(parents=True, exist_ok=True)


_ORIGINAL_COPYFILE = shutil.copyfile


def _copyfile_with_parent_dirs(src, dst, *args, **kwargs):
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    return _ORIGINAL_COPYFILE(src, dst, *args, **kwargs)


shutil.copyfile = _copyfile_with_parent_dirs


datas = iter_datas()
datas += collect_data_files("faster_whisper")
datas += collect_data_files("kokoro_onnx")
datas += collect_data_files("espeakng_loader")
datas = [(src, dest) for src, dest in datas if Path(src).is_file()]
hiddenimports = sorted(set(
    collect_submodules("PyQt6")
    + collect_submodules("faster_whisper")
    + collect_submodules("ctranslate2")
    + collect_submodules("kokoro_onnx")
    + collect_submodules("phonemizer")
    + collect_submodules("espeakng_loader")
    + [
        "numpy.typing",
        "numpy._typing",
        "numpy._typing._add_docstring",
        "numpy._typing._array_like",
        "numpy._typing._char_codes",
        "numpy._typing._dtype_like",
        "numpy._typing._extended_precision",
        "numpy._typing._nbit",
        "numpy._typing._nbit_base",
        "numpy._typing._nested_sequence",
        "numpy._typing._scalars",
        "numpy._typing._shape",
        "numpy._typing._ufunc",
    ]
    + [
        "api",
        "agents",
        "behavior_hooks",
        "brains.brain",
        "brains.brain_claude",
        "brains.brain_gemini",
        "brains.brain_ollama",
        "briefing",
        "browser",
        "call_privacy",
        "camera",
        "desktop.bridge",
        "config",
        "conversation_context",
        "cost_policy",
        "desktop.device_panel",
        "evals",
        "google_services",
        "hardware",
        "desktop.hotkeys",
        "interview_profile",
        "jarvis_daemon",
        "learner",
        "local_runtime.local_beta",
        "local_runtime.local_model_automation",
        "local_runtime.local_model_eval",
        "local_runtime.local_training",
        "local_runtime.model_fleet",
        "local_runtime.local_kokoro_tts",
        "local_runtime.local_stt",
        "local_runtime.local_tts",
        "local_runtime.local_model_benchmark",
        "meeting_listener",
        "meeting_controller",
        "memory",
        "messages",
        "model_router",
        "notes",
        "operative",
        "orchestrator",
        "desktop.overlay",
        "prompt_modifiers",
        "provider_priority",
        "research",
        "router",
        "desktop.screen_capture",
        "self_improve",
        "runtime_state",
        "semantic_memory",
        "skill_factory",
        "skills",
        "source_ingest",
        "specialized_agents",
        "stealth",
        "terminal",
        "tools",
        "usage_tracker",
        "vault",
        "voice",
        "wiki_builder",
    ]
))


a = Analysis(
    ["main.py"],
    pathex=[str(ROOT)],
    binaries=collect_dynamic_libs("espeakng_loader"),
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    exclude_binaries=False,
    name="Jarvis",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

app = BUNDLE(
    exe,
    name="Jarvis.app",
    icon=str(ICON) if ICON.is_file() else None,
    bundle_identifier="com.truthseeker.jarvis",
    info_plist={
        "CFBundleName": "Jarvis",
        "CFBundleDisplayName": "Jarvis",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription": "Jarvis needs microphone access so it can hear your voice commands and conversations you explicitly ask it to process.",
        "NSCameraUsageDescription": "Jarvis needs camera access for webcam vision features you explicitly trigger.",
        "NSContactsUsageDescription": "Jarvis needs Contacts access to look up people when you ask it to message or call them.",
        "NSAppleEventsUsageDescription": "Jarvis needs automation access to control apps like Safari, Messages, and Terminal when you ask it to take action.",
    },
)
