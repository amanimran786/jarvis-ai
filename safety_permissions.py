from __future__ import annotations

import behavior_hooks as hooks


def can_run_shell(command: str, *, admin: bool = False, cwd: str | None = None) -> dict:
    return hooks.pre_shell_command(command, cwd=cwd, admin=admin)


def can_write_file(path: str, *, source: str) -> dict:
    return hooks.pre_file_write(path, source=source)


def after_write(path: str, *, source: str) -> dict:
    return hooks.post_file_write(path, source=source)


def can_self_improve(target: str) -> dict:
    return hooks.pre_self_improve(target)
