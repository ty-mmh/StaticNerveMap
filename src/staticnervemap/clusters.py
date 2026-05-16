from __future__ import annotations

from collections import defaultdict

from .model import Entrypoint, FileEntry, Relation
from .postprocess_lookup import FileLookup, build_file_lookup, resolve_relation_file_ids


def build_clusters(
    files: list[FileEntry],
    relations: list[Relation],
    entrypoints: list[Entrypoint] | None = None,
) -> list[dict[str, object]]:
    lookup = build_file_lookup(files)
    local_files = lookup.local_files
    file_ids = set(file_entry.id for file_entry in local_files)
    entrypoints = entrypoints or []
    resolved_relations = resolve_relation_file_ids(relations, lookup)

    clusters: list[dict[str, object]] = []

    boundary_members = _boundary_members(lookup, resolved_relations, entrypoints)
    if boundary_members:
        clusters.append(
            {
                "id": "cluster:boundary-surface",
                "name": "boundary-surface",
                "description": "Entry, route, command, and UI boundary files.",
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
                "description": f"Files grouped by the `{prefix}` package or path prefix.",
                "members": sorted(members),
            }
        )

    support_members = _support_members(local_files)
    if len(support_members) >= 2:
        clusters.append(
            {
                "id": "cluster:support",
                "name": "support",
                "description": "Configuration, helpers, and shared support files.",
                "members": support_members,
            }
        )

    if not clusters and local_files:
        first = sorted(local_files, key=lambda file_entry: file_entry.path)[0]
        clusters.append(
            {
                "id": "cluster:singleton",
                "name": "singleton",
                "description": "Fallback single-file cluster.",
                "members": [first.id],
            }
        )

    relation_density = _cluster_relation_density(clusters, resolved_relations, file_ids)
    for cluster in clusters:
        density = relation_density.get(cluster["id"], 0)
        cluster["signal"] = "high" if density >= 6 else "medium" if density >= 2 else "low"

    return _dedupe_clusters(clusters)


def _boundary_members(
    lookup: FileLookup,
    resolved_relations: list[tuple[Relation, str | None, str | None]],
    entrypoints: list[Entrypoint],
) -> list[str]:
    members: set[str] = set()

    for entry in entrypoints:
        file_id = lookup.resolve_file_id(entry.symbol_id)
        if file_id:
            members.add(file_id)

    for rel, from_file, to_file in resolved_relations:
        if rel.type not in {"ui_binds", "route_binds", "command_binds"}:
            continue
        if from_file:
            members.add(from_file)
        if to_file:
            members.add(to_file)

    return sorted(
        member
        for member in members
        if lookup.file_map.get(member)
        and lookup.file_map[member].role in {"entrypoint_candidate", "configuration", "source"}
    )


def _prefix_groups(files: list[FileEntry]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for file_entry in files:
        groups[_cluster_prefix(file_entry)].append(file_entry.id)
    return dict(sorted(groups.items()))


def _cluster_prefix(file_entry: FileEntry) -> str:
    module = file_entry.module
    if module:
        return module.split(".", 1)[0]
    path = file_entry.path
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2:
        return parts[0]
    return path


def _support_members(files: list[FileEntry]) -> list[str]:
    has_package_dirs = any("/" in file_entry.path for file_entry in files if file_entry.role != "test")
    members: set[str] = set()
    for file_entry in files:
        if file_entry.role == "configuration" or _is_support_name(file_entry.path):
            members.add(file_entry.id)
            continue
        if has_package_dirs and file_entry.role == "source" and "/" not in file_entry.path:
            members.add(file_entry.id)
    return sorted(members)


def _is_support_name(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return name in {
        "__init__.py",
        "config.py",
        "settings.py",
        "utils.py",
        "helpers.py",
        "common.py",
    }


def _cluster_relation_density(
    clusters: list[dict[str, object]],
    resolved_relations: list[tuple[Relation, str | None, str | None]],
    file_ids: set[str],
) -> dict[str, int]:
    densities = {str(cluster["id"]): 0 for cluster in clusters}
    clusters_by_file: dict[str, set[str]] = defaultdict(set)
    for cluster in clusters:
        cluster_id = str(cluster["id"])
        for member in cluster["members"]:
            clusters_by_file[str(member)].add(cluster_id)

    for _, from_file, to_file in resolved_relations:
        if from_file not in file_ids or to_file not in file_ids:
            continue
        impacted = clusters_by_file.get(from_file, set()) | clusters_by_file.get(to_file, set())
        for cluster_id in impacted:
            densities[cluster_id] += 1
    return densities


def _dedupe_clusters(clusters: list[dict[str, object]]) -> list[dict[str, object]]:
    seen: set[str] = set()
    deduped: list[dict[str, object]] = []
    for cluster in clusters:
        cluster_id = str(cluster["id"])
        if cluster_id in seen:
            continue
        seen.add(cluster_id)
        deduped.append(cluster)
    return deduped
