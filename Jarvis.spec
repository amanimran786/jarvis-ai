# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


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


datas = iter_datas()
hiddenimports = sorted(set(
    collect_submodules("PyQt6")
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
    binaries=[],
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
    [],
    exclude_binaries=True,
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
    codesign_identity="-",
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Jarvis",
)

app = BUNDLE(
    coll,
    name="Jarvis.app",
    icon=str(ICON) if ICON.is_file() else None,
    bundle_identifier="com.truthseeker.jarvis",
    info_plist={
        "CFBundleName": "Jarvis",
        "CFBundleDisplayName": "Jarvis",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "NSHighResolutionCapable": True,
    },
)
