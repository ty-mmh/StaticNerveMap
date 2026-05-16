from __future__ import annotations

from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from .model import Entrypoint, FileEntry, Symbol


def collect_metadata_entrypoints(
    repo_root: Path,
    files: list[FileEntry],
    symbols: list[Symbol],
) -> list[Entrypoint]:
    pyproject_path = repo_root / "pyproject.toml"
    if not pyproject_path.exists():
        return []

    data = _load_pyproject(pyproject_path)
    if not data:
        return []

    module_to_file = {file_entry.module: file_entry for file_entry in files}
    symbol_by_qn = {symbol.qualified_name: symbol for symbol in symbols}

    entrypoints: list[Entrypoint] = []
    project = data.get("project")
    if not isinstance(project, dict):
        return []

    scripts = project.get("scripts")
    if isinstance(scripts, dict):
        for name, target in sorted(scripts.items()):
            if not isinstance(name, str) or not isinstance(target, str):
                continue
            resolved = _resolve_entrypoint_target(target, module_to_file, symbol_by_qn)
            if resolved is None:
                continue
            entrypoints.append(
                Entrypoint(
                    id=f"entry:metadata:script:{_safe_entry_name(name)}",
                    symbol_id=resolved,
                    kind="metadata_script",
                    priority=1,
                    reason=f"pyproject.toml [project.scripts].{name} -> {target}",
                )
            )

    entry_groups = project.get("entry-points")
    if isinstance(entry_groups, dict):
        for group_name, group in sorted(entry_groups.items()):
            if not isinstance(group_name, str) or not isinstance(group, dict):
                continue
            for name, target in sorted(group.items()):
                if not isinstance(name, str) or not isinstance(target, str):
                    continue
                resolved = _resolve_entrypoint_target(target, module_to_file, symbol_by_qn)
                if resolved is None:
                    continue
                entrypoints.append(
                    Entrypoint(
                        id=(
                            f"entry:metadata:entry_point:"
                            f"{_safe_entry_name(group_name)}:{_safe_entry_name(name)}"
                        ),
                        symbol_id=resolved,
                        kind=_metadata_entrypoint_kind(group_name),
                        priority=1,
                        reason=(
                            f"pyproject.toml [project.entry-points.{group_name}]."
                            f"{name} -> {target}"
                        ),
                    )
                )

    return _dedupe_entrypoints(entrypoints)


def _load_pyproject(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_entrypoint_target(
    target: str,
    module_to_file: dict[str, FileEntry],
    symbol_by_qn: dict[str, Symbol],
) -> str | None:
    module_name, _, attr_path = target.partition(":")
    module_name = module_name.strip()
    attr_path = attr_path.strip()
    if not module_name:
        return None

    if attr_path:
        attr_parts = [part for part in attr_path.split(".") if part]
        while attr_parts:
            qn = f"{module_name}.{'.'.join(attr_parts)}"
            symbol = symbol_by_qn.get(qn)
            if symbol is not None:
                return symbol.id
            attr_parts.pop()

    file_entry = module_to_file.get(module_name)
    if file_entry is not None:
        return file_entry.id
    return None


def _metadata_entrypoint_kind(group_name: str) -> str:
    if group_name == "pytest11":
        return "metadata_pytest_plugin"
    return "metadata_entrypoint"


def _safe_entry_name(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in name)


def _dedupe_entrypoints(entrypoints: list[Entrypoint]) -> list[Entrypoint]:
    seen: set[tuple[str, str, str]] = set()
    deduped: list[Entrypoint] = []
    for entrypoint in entrypoints:
        key = (entrypoint.id, entrypoint.symbol_id, entrypoint.kind)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entrypoint)
    return deduped
