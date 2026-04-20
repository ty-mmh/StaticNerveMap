from __future__ import annotations

from collections import defaultdict

from .model import Entrypoint, FileEntry, Relation


def build_clusters(
    files: list[FileEntry],
    relations: list[Relation],
    entrypoints: list[Entrypoint] | None = None,
) -> list[dict[str, object]]:
    local_files = [f for f in files if f.id.startswith("file:")]
    file_ids = {f.id for f in local_files}
    entrypoints = entrypoints or []

    clusters: list[dict[str, object]] = []

    boundary_members = _boundary_members(local_files, relations, entrypoints)
    if boundary_members:
        clusters.append(
            {
                "id": "cluster:boundary-surface",
                "name": "boundary-surface",
                "description": "エントリポイントや bind 関連から見える入口層クラスタ。",
                "members": boundary_members,
            }
        )

    for prefix, members in _prefix_groups(local_files).items():
        if len(members) < 2:
            continue
        clusters.append(
            {
                "id": f"cluster:{prefix}",
                "name": prefix,
                "description": f"モジュール接頭辞 `{prefix}` に基づく構造クラスタ。",
                "members": sorted(members),
            }
        )

    support_members = _support_members(local_files)
    if len(support_members) >= 2:
        clusters.append(
            {
                "id": "cluster:support",
                "name": "support",
                "description": "設定・補助処理・ユーティリティに寄った支援クラスタ。",
                "members": support_members,
            }
        )

    if not clusters and local_files:
        first = sorted(local_files, key=lambda f: f.path)[0]
        clusters.append(
            {
                "id": "cluster:singleton",
                "name": "singleton",
                "description": "単一ファイル中心の最小クラスタ。",
                "members": [first.id],
            }
        )

    relation_density = _cluster_relation_density(clusters, relations, file_ids, local_files)
    for cluster in clusters:
        density = relation_density.get(cluster["id"], 0)
        cluster["signal"] = "high" if density >= 6 else "medium" if density >= 2 else "low"

    return _dedupe_clusters(clusters)


def _boundary_members(
    files: list[FileEntry],
    relations: list[Relation],
    entrypoints: list[Entrypoint],
) -> list[str]:
    file_map = {f.id: f for f in files}
    members: set[str] = set()

    for entry in entrypoints:
        file_id = _symbol_to_file_id(entry.symbol_id, files)
        if file_id:
            members.add(file_id)

    for rel in relations:
        if rel.type not in {"ui_binds", "route_binds", "command_binds"}:
            continue
        from_file = _symbol_to_file_id(rel.from_id, files)
        to_file = _symbol_to_file_id(rel.to_id, files)
        if from_file:
            members.add(from_file)
        if to_file:
            members.add(to_file)

    focused = sorted(
        member for member in members
        if file_map.get(member) and file_map[member].role in {"entrypoint_candidate", "configuration", "source"}
    )
    return focused


def _prefix_groups(files: list[FileEntry]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for file_entry in files:
        prefix = _cluster_prefix(file_entry)
        groups[prefix].append(file_entry.id)
    return dict(sorted(groups.items()))


def _cluster_prefix(file_entry: FileEntry) -> str:
    path = file_entry.path
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and parts[0] == "src":
        return f"{parts[0]}.{parts[1]}"
    if len(parts) >= 2:
        return parts[0]
    module = file_entry.module
    if "." in module:
        first, second = module.split(".", 1)
        return f"{first}.{second.split('.', 1)[0]}"
    return module


def _support_members(files: list[FileEntry]) -> list[str]:
    return sorted(
        f.id
        for f in files
        if f.role == "configuration" or _is_support_name(f.path)
    )


def _is_support_name(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return name in {
        "__init__.py",
        "config.py",
        "settings.py",
        "utils.py",
        "helpers.py",
        "common.py",
        "resample_wavs.py",
    }


def _cluster_relation_density(
    clusters: list[dict[str, object]],
    relations: list[Relation],
    file_ids: set[str],
    files: list[FileEntry],
) -> dict[str, int]:
    densities: dict[str, int] = {}
    for cluster in clusters:
        members = set(cluster["members"])
        densities[cluster["id"]] = sum(
            1
            for rel in relations
            if _symbol_to_file_id(rel.from_id, files) in file_ids
            and _symbol_to_file_id(rel.to_id, files) in file_ids
            and (
                _symbol_to_file_id(rel.from_id, files) in members
                or _symbol_to_file_id(rel.to_id, files) in members
            )
        )
    return densities


def _symbol_to_file_id(symbol_or_file_id: str, files: list[FileEntry]) -> str | None:
    file_map = {f.id: f for f in files}
    if symbol_or_file_id in file_map:
        return symbol_or_file_id
    candidates: list[tuple[int, str]] = []
    for file_entry in files:
        module = file_entry.module
        if (
            symbol_or_file_id.startswith(f"function:{module}.")
            or symbol_or_file_id.startswith(f"method:{module}.")
            or symbol_or_file_id.startswith(f"class:{module}.")
        ):
            candidates.append((len(module), file_entry.id))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]
    return None


def _dedupe_clusters(clusters: list[dict[str, object]]) -> list[dict[str, object]]:
    seen: set[str] = set()
    deduped: list[dict[str, object]] = []
    for cluster in clusters:
        cid = str(cluster["id"])
        if cid in seen:
            continue
        seen.add(cid)
        deduped.append(cluster)
    return deduped
