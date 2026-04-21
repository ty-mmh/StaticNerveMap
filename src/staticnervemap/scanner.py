from __future__ import annotations

import ast
import re
from pathlib import Path

from .model import FileEntry

CORE_EXCLUDED_DIRS = {
    "__pycache__",
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "build",
    "dist",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "assets",
    "output",
    "corpus",
    "presets",
    ".staticnervemap",
}

DEFAULT_EXCLUDED_DIRS = {
    *CORE_EXCLUDED_DIRS,
    "tests",
    ".agent",
    ".claude",
    ".github",
    "script",
    "rootfs",
}

ENTRY_NAMES = {"app.py", "main.py", "__main__.py", "run.py", "server.py"}
CONFIG_NAMES = {"config.py", "settings.py", "conf.py"}


def _rel_posix(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _module_name(rel_posix: str) -> str:
    stem = rel_posix[:-3] if rel_posix.endswith(".py") else rel_posix
    parts = [p for p in stem.split("/") if p and p != "__init__"]
    return ".".join(parts)


def _role_for(rel_posix: str) -> str:
    name = rel_posix.rsplit("/", 1)[-1]
    if name in ENTRY_NAMES:
        return "entrypoint_candidate"
    if name in CONFIG_NAMES:
        return "configuration"
    if rel_posix.startswith("tests/") or name.startswith("test_") or name.endswith("_test.py"):
        return "test"
    return "source"


def _load_gitignore_excluded_dirs(root: Path) -> set[str]:
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        return set()

    excluded: set[str] = set()
    try:
        lines = gitignore.read_text(encoding="utf-8").splitlines()
    except OSError:
        return excluded

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        if "*" in line or "?" in line or "[" in line:
            continue
        normalized = line.replace("\\", "/").strip("/")
        if not normalized or "/" in normalized:
            continue
        if normalized.endswith(".py"):
            continue
        excluded.add(normalized)
    return excluded


def _top_level_dir(rel_posix: str) -> str | None:
    parts = rel_posix.split("/", 1)
    if len(parts) == 2:
        return parts[0]
    return None


def _choose_primary_packages(files: list[FileEntry]) -> set[str]:
    package_counts: dict[str, int] = {}
    package_has_init: set[str] = set()
    for file_entry in files:
        top = _top_level_dir(file_entry.path)
        if top is None:
            continue
        package_counts[top] = package_counts.get(top, 0) + 1
        if file_entry.path == f"{top}/__init__.py":
            package_has_init.add(top)

    candidates = [
        (count, name)
        for name, count in package_counts.items()
        if name in package_has_init
    ]
    if not candidates:
        return set()
    candidates.sort(reverse=True)
    return {candidates[0][1]}


def _prioritize_files(files: list[FileEntry]) -> list[FileEntry]:
    def _key(file_entry: FileEntry) -> tuple[int, str]:
        if file_entry.role == "entrypoint_candidate":
            return (0, file_entry.path)
        if file_entry.role == "configuration":
            return (1, file_entry.path)
        if file_entry.path.count("/") == 0:
            return (2, file_entry.path)
        return (3, file_entry.path)

    return sorted(files, key=_key)


def _build_module_prefix_index(module_names: set[str]) -> dict[str, set[str]]:
    index: dict[str, set[str]] = {}
    for module_name in module_names:
        parts = module_name.split(".")
        for i in range(1, len(parts) + 1):
            prefix = ".".join(parts[:i])
            index.setdefault(prefix, set()).add(module_name)
    return index


def _collect_local_import_modules_ast(
    root: Path,
    file_entry: FileEntry,
    module_names: set[str],
    prefix_index: dict[str, set[str]],
) -> set[str]:
    path = root / file_entry.path
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return set()

    imported: set[str] = set()
    package_parts = file_entry.module.split(".")[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                candidate = alias.name
                if candidate in module_names:
                    imported.add(candidate)
                    continue
                imported.update(prefix_index.get(candidate, set()))
        elif isinstance(node, ast.ImportFrom):
            base = ""
            if node.level:
                keep = max(len(package_parts) - (node.level - 1), 0)
                base_parts = package_parts[:keep]
            else:
                base_parts = []
            if node.module:
                base_parts = [*base_parts, *node.module.split(".")]
            base = ".".join(part for part in base_parts if part)
            if base and base in module_names:
                imported.add(base)
            for alias in node.names:
                if alias.name == "*":
                    if base in module_names:
                        imported.add(base)
                    continue
                candidates = []
                if base:
                    candidates.append(f"{base}.{alias.name}")
                candidates.append(alias.name)
                for candidate in candidates:
                    if candidate in module_names:
                        imported.add(candidate)
                        break
                else:
                    if base:
                        imported.update(prefix_index.get(base, set()))
    return imported


def _collect_local_import_modules(
    root: Path,
    file_entry: FileEntry,
    module_names: set[str],
    prefix_index: dict[str, set[str]],
) -> set[str]:
    path = root / file_entry.path
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()

    if "\\\n" in text or re.search(r"import\s*\(", text):
        return _collect_local_import_modules_ast(root, file_entry, module_names, prefix_index)

    imported: set[str] = set()
    package_parts = file_entry.module.split(".")[:-1]
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("import "):
            clause = line[7:]
            for part in clause.split(","):
                candidate = part.strip().split(" as ", 1)[0].strip()
                if not candidate:
                    continue
                if candidate in module_names:
                    imported.add(candidate)
                    continue
                imported.update(prefix_index.get(candidate, set()))
            continue
        if not line.startswith("from ") or " import " not in line:
            continue
        lhs, rhs = line[5:].split(" import ", 1)
        level = len(lhs) - len(lhs.lstrip("."))
        module_name = lhs[level:].strip()
        if level:
            keep = max(len(package_parts) - (level - 1), 0)
            base_parts = package_parts[:keep]
        else:
            base_parts = []
        if module_name:
            base_parts = [*base_parts, *module_name.split(".")]
        base = ".".join(part for part in base_parts if part)
        if base and base in module_names:
            imported.add(base)
        names = [part.strip() for part in rhs.split(",")]
        for name in names:
            alias_name = name.split(" as ", 1)[0].strip()
            if not alias_name:
                continue
            if alias_name == "*":
                if base:
                    imported.update(prefix_index.get(base, set()))
                continue
            candidates = []
            if base:
                candidates.append(f"{base}.{alias_name}")
            candidates.append(alias_name)
            matched = False
            for candidate in candidates:
                if candidate in module_names:
                    imported.add(candidate)
                    matched = True
                    break
            if not matched and base:
                imported.update(prefix_index.get(base, set()))
    return imported


def _seed_focused_modules(files: list[FileEntry]) -> set[str]:
    modules: set[str] = set()
    for file_entry in files:
        if file_entry.role in {"entrypoint_candidate", "configuration"}:
            modules.add(file_entry.module)
            continue
        if file_entry.path.count("/") == 0:
            modules.add(file_entry.module)
    return modules


def _expand_reachable_modules(root: Path, files: list[FileEntry]) -> set[str]:
    module_to_file = {file_entry.module: file_entry for file_entry in files}
    module_names = set(module_to_file)
    prefix_index = _build_module_prefix_index(module_names)
    reachable = set(_seed_focused_modules(files))
    queue = list(reachable)
    while queue:
        module = queue.pop()
        file_entry = module_to_file.get(module)
        if file_entry is None:
            continue
        for imported in _collect_local_import_modules(root, file_entry, module_names, prefix_index):
            if imported not in reachable:
                reachable.add(imported)
                queue.append(imported)
    return reachable


def compute_excluded_dirs(
    root: Path,
    *,
    extra_excluded_dirs: set[str] | None = None,
    use_gitignore: bool = True,
    scan_mode: str = "default",
) -> set[str]:
    if scan_mode not in {"full", "default", "focused"}:
        raise ValueError(f"unknown scan mode: {scan_mode}")
    excluded_dirs = set(CORE_EXCLUDED_DIRS if scan_mode == "full" else DEFAULT_EXCLUDED_DIRS)
    if extra_excluded_dirs:
        excluded_dirs.update(extra_excluded_dirs)
    if use_gitignore and scan_mode != "full":
        excluded_dirs.update(_load_gitignore_excluded_dirs(root))
    return excluded_dirs


def scan_repo(
    root: Path,
    *,
    extra_excluded_dirs: set[str] | None = None,
    use_gitignore: bool = True,
    scan_mode: str = "default",
) -> list[FileEntry]:
    root = root.resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {root}")

    excluded_dirs = compute_excluded_dirs(
        root,
        extra_excluded_dirs=extra_excluded_dirs,
        use_gitignore=use_gitignore,
        scan_mode=scan_mode,
    )

    files: list[FileEntry] = []
    for dirpath, dirnames, filenames in _walk(root):
        if scan_mode == "full":
            dirnames[:] = [d for d in dirnames if d not in excluded_dirs]
        else:
            dirnames[:] = [d for d in dirnames if d not in excluded_dirs and not d.startswith(".")]
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            fpath = Path(dirpath) / fname
            rel = _rel_posix(root, fpath)
            module = _module_name(rel) or fname[:-3]
            files.append(
                FileEntry(
                    id=f"file:{rel}",
                    path=rel,
                    language="python",
                    module=module,
                    role=_role_for(rel),
                )
            )

    files = _prioritize_files(files)
    if scan_mode != "focused":
        return files

    primary_packages = _choose_primary_packages(files)
    reachable_modules = _expand_reachable_modules(root, files)
    focused_files: list[FileEntry] = []
    for file_entry in files:
        top = _top_level_dir(file_entry.path)
        if file_entry.role in {"entrypoint_candidate", "configuration"}:
            focused_files.append(file_entry)
            continue
        if file_entry.module in reachable_modules:
            focused_files.append(file_entry)
            continue
        if top is None:
            focused_files.append(file_entry)
            continue
        if top in primary_packages:
            focused_files.append(file_entry)
    return focused_files


def _walk(root: Path):
    import os

    for dirpath, dirnames, filenames in os.walk(root):
        yield dirpath, dirnames, filenames
