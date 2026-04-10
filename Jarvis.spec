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
}
EXCLUDE_EXTS = {
    ".py",
    ".pyc",
    ".pyo",
    ".spec",
}


def iter_datas():
    datas = []
    env_path = ROOT / ".env"
    if env_path.is_file():
        datas.append((str(env_path), "."))
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if not path.is_file():
            continue
        if path.suffix in EXCLUDE_EXTS:
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
        "brain",
        "brain_claude",
        "brain_gemini",
        "brain_ollama",
        "briefing",
        "browser",
        "call_privacy",
        "camera",
        "bridge",
        "config",
        "conversation_context",
        "cost_policy",
        "device_panel",
        "evals",
        "google_services",
        "hardware",
        "hotkeys",
        "interview_profile",
        "jarvis_daemon",
        "learner",
        "local_beta",
        "local_model_automation",
        "local_model_eval",
        "local_training",
        "meeting_listener",
        "meeting_controller",
        "memory",
        "messages",
        "model_router",
        "notes",
        "operative",
        "orchestrator",
        "overlay",
        "prompt_modifiers",
        "provider_priority",
        "research",
        "router",
        "screen_capture",
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
    codesign_identity=None,
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
