from __future__ import annotations

from collections import defaultdict, deque

from .model import Entrypoint, Relation, Symbol
from .postprocess_lookup import build_symbol_file_map


MAX_PATH_DEPTH = 4
MAX_PATHS_PER_GROUP = 2


def build_modification_paths(
    relations: list[Relation],
    symbols: list[Symbol],
    entrypoints: list[Entrypoint] | None = None,
    change_targets: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    entrypoints = entrypoints or []
    change_targets = change_targets or []
    symbol_to_file = build_symbol_file_map(symbols)
    symbol_by_id = {symbol.id: symbol for symbol in symbols}
    runtime_primary: set[str] = set()
    subsystem_primary: set[str] = set()
    for target in change_targets:
        target_id = str(target.get("id") or "")
        if target_id == "change:runtime-core":
            runtime_primary.update(str(file_id) for file_id in target.get("primary_files", []))
        elif target_id.startswith("change:subsystem:"):
            subsystem_primary.update(str(file_id) for file_id in target.get("primary_files", []))
    destination_files = runtime_primary | subsystem_primary

    call_graph: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for rel in relations:
        if rel.type != "calls":
            continue
        call_graph[rel.from_id].append((rel.to_id, rel.id))

    paths: list[dict[str, object]] = []
    seen_keys: set[tuple[str, str, tuple[str, ...]]] = set()

    for rel in relations:
        if rel.type != "ui_binds":
            continue
        handler_id = rel.to_id
        if not handler_id.startswith(("function:", "method:")):
            continue

        trigger_source = rel.from_id
        details = rel.details or {}
        event_method = str(details.get("event_method", ""))
        event_source = str(details.get("event_source", ""))
        discovered = _discover_runtime_paths(
            handler_id,
            call_graph,
            symbol_to_file,
            destination_files,
        )
        for node_path, relation_path, target_file in discovered:
            dedupe_key = (trigger_source, target_file, tuple(node_path))
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            paths.append(
                {
                    "id": f"path:{rel.id}:{len(paths)+1}",
                    "kind": "ui_to_runtime",
                    "trigger_relation_id": rel.id,
                    "trigger_source": trigger_source,
                    "handler_symbol_id": handler_id,
                    "event_method": event_method,
                    "event_source": event_source,
                    "path": node_path,
                    "relation_path": relation_path,
                    "target_file": target_file,
                    "priority": _path_priority(
                        node_path, target_file, runtime_primary, subsystem_primary
                    ),
                }
            )

    for entrypoint in entrypoints:
        handler_id = entrypoint.symbol_id
        if not handler_id.startswith(("function:", "method:")):
            continue
        symbol = symbol_by_id.get(handler_id)
        event_source = symbol.name if symbol is not None else handler_id.rsplit(".", 1)[-1]
        discovered = _discover_runtime_paths(
            handler_id,
            call_graph,
            symbol_to_file,
            destination_files,
        )
        for node_path, relation_path, target_file in discovered:
            dedupe_key = (entrypoint.id, target_file, tuple(node_path))
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            paths.append(
                {
                    "id": f"path:{entrypoint.id}:{len(paths)+1}",
                    "kind": "entrypoint_to_runtime",
                    "trigger_relation_id": None,
                    "trigger_source": entrypoint.id,
                    "handler_symbol_id": handler_id,
                    "event_method": entrypoint.kind,
                    "event_source": event_source,
                    "path": node_path,
                    "relation_path": relation_path,
                    "target_file": target_file,
                    "priority": _path_priority(
                        node_path, target_file, runtime_primary, subsystem_primary
                    ),
                }
            )

    paths = _prune_paths(paths)

    paths.sort(
        key=lambda item: (
            int(item["priority"]),
            len(item["path"]),
            str(item["event_source"]),
            str(item["target_file"]),
        )
    )
    return paths


def _discover_runtime_paths(
    handler_id: str,
    call_graph: dict[str, list[tuple[str, str]]],
    symbol_to_file: dict[str, str],
    destination_files: set[str],
    max_depth: int = MAX_PATH_DEPTH,
) -> list[tuple[list[str], list[str], str]]:
    results: list[tuple[list[str], list[str], str]] = []
    queue: deque[tuple[str, list[str], list[str], int]] = deque()
    queue.append((handler_id, [handler_id], [], 0))
    visited: set[tuple[str, int]] = {(handler_id, 0)}

    while queue:
        current, node_path, rel_path, depth = queue.popleft()
        current_file = symbol_to_file.get(current)
        if current_file in destination_files and len(node_path) > 1:
            results.append((node_path, rel_path, current_file))
        if depth >= max_depth:
            continue
        for next_id, rel_id in call_graph.get(current, []):
            state = (next_id, depth + 1)
            if state in visited:
                continue
            visited.add(state)
            queue.append((next_id, node_path + [next_id], rel_path + [rel_id], depth + 1))

    return results


def _path_priority(
    node_path: list[str],
    target_file: str,
    runtime_primary: set[str],
    subsystem_primary: set[str],
) -> int:
    last_is_method = node_path[-1].startswith("method:")
    if target_file in runtime_primary:
        return 1 if last_is_method else 2
    if target_file in subsystem_primary:
        return 3 if last_is_method else 4
    return 5


def _prune_paths(paths: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for path in paths:
        grouped[(str(path["event_source"]), str(path["target_file"]))].append(path)

    kept: list[dict[str, object]] = []
    for _, group in grouped.items():
        group.sort(
            key=lambda item: (
                int(item["priority"]),
                len(item["path"]),
                0 if str(item["path"][-1]).startswith("method:") else 1,
                str(item["handler_symbol_id"]),
            )
        )

        selected: list[dict[str, object]] = []
        seen_prefixes: list[tuple[str, ...]] = []
        for item in group:
            node_tuple = tuple(str(node) for node in item["path"])
            if any(node_tuple[: len(prefix)] == prefix for prefix in seen_prefixes):
                continue
            selected.append(item)
            seen_prefixes.append(node_tuple)
            if len(selected) >= MAX_PATHS_PER_GROUP:
                break
        kept.extend(selected)
    return kept
