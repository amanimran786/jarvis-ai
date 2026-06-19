import ast
from pathlib import Path


SPEC_PATH = Path(__file__).resolve().parents[1] / "Jarvis.spec"


def _load_iter_datas(root):
    tree = ast.parse(SPEC_PATH.read_text(encoding="utf-8"))
    selected = []
    names = {
        "EXCLUDE_DIRS",
        "EXCLUDE_EXTS",
        "EXCLUDE_FILES",
        "EXCLUDE_FILE_SUFFIXES",
    }
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in names
            for target in node.targets
        ):
            selected.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name == "iter_datas":
            selected.append(node)

    namespace = {"Path": Path, "ROOT": root}
    exec(compile(ast.Module(body=selected, type_ignores=[]), SPEC_PATH, "exec"), namespace)
    return namespace["iter_datas"]


def test_iter_datas_excludes_private_runtime_artifacts(tmp_path):
    private_paths = [
        tmp_path / "projects.db",
        tmp_path / "projects.db-shm",
        tmp_path / "projects.db-wal",
        tmp_path / "contact_aliases.json",
        tmp_path / "session_state.json",
        tmp_path / "usage_state.json",
        tmp_path / ".jarvis_runtime.json",
        tmp_path / "logs" / "jarvis.log",
        tmp_path / "runtime" / "jarvis_tasks.sqlite3",
        tmp_path / ".worktrees" / "other" / "private.txt",
    ]
    for path in private_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private", encoding="utf-8")

    included_sources = {Path(src).relative_to(tmp_path) for src, _ in _load_iter_datas(tmp_path)()}

    assert included_sources.isdisjoint(path.relative_to(tmp_path) for path in private_paths)


def test_iter_datas_keeps_required_non_python_data(tmp_path):
    asset = tmp_path / "assets" / "jarvis.icns"
    asset.parent.mkdir()
    asset.write_text("icon", encoding="utf-8")

    datas = _load_iter_datas(tmp_path)()

    assert (str(asset), "assets") in datas
