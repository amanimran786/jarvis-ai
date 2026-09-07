from pathlib import Path

from scripts.install_v2_local import LABEL, build_plist


def test_v2_launch_agent_is_loopback_offline_and_credential_free(tmp_path: Path):
    plist = build_plist(
        executable=Path("/repo/venv/bin/mlx_lm.server"),
        model_path=Path("/models/qwen"),
        log_dir=tmp_path,
    )

    assert plist["Label"] == LABEL
    arguments = plist["ProgramArguments"]
    assert arguments[arguments.index("--host") + 1] == "127.0.0.1"
    assert arguments[arguments.index("--port") + 1] == "8080"
    assert plist["EnvironmentVariables"]["HF_HUB_OFFLINE"] == "1"
    assert plist["EnvironmentVariables"]["TRANSFORMERS_OFFLINE"] == "1"
    assert not any("key" in key.lower() or "token" in key.lower() for key in plist)
