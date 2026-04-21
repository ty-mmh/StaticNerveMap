from __future__ import annotations

from collections import defaultdict
from itertools import count

from .model import Entrypoint, FileEntry, Relation
from .postprocess_lookup import build_file_lookup, resolve_relation_file_ids


def build_change_targets(
    files: list[FileEntry],
    relations: list[Relation],
    entrypoints: list[Entrypoint] | None = None,
    api_contracts: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    lookup = build_file_lookup(files)
    local_files = lookup.local_files
    local_ids = lookup.local_file_ids
    entrypoints = entrypoints or []
    api_contracts = api_contracts or []

    entrypoint_file_ids = {
        file_id
        for file_id in (lookup.resolve_file_id(entry.symbol_id) for entry in entrypoints)
        if file_id is not None
    }
    resolved_relations = resolve_relation_file_ids(relations, lookup)

    score: dict[str, float] = defaultdict(float)
    related_relation_ids: dict[str, list[str]] = defaultdict(list)
    boundary_file_ids = set(entrypoint_file_ids)
    boundary_bind_files: set[str] = set()
    direct_boundary_calls: dict[str, float] = defaultdict(float)
    inbound_from_boundary: dict[str, float] = defaultdict(float)
    outbound_to_runtime: dict[str, float] = defaultdict(float)
    calls_from_to: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
    calls_to_from: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
    boundary_bind_relation_ids: dict[str, list[str]] = defaultdict(list)

    for rel, from_file_id, to_file_id in resolved_relations:
        if rel.type in {"calls", "imports", "inherits"}:
            if from_file_id in local_ids and to_file_id in local_ids:
                score[to_file_id] += rel.confidence
                related_relation_ids[to_file_id].append(rel.id)

        if rel.type in {"ui_binds", "route_binds", "command_binds"}:
            if from_file_id in local_ids:
                score[from_file_id] += 0.35
                boundary_file_ids.add(from_file_id)
                boundary_bind_files.add(from_file_id)
                boundary_bind_relation_ids[from_file_id].append(rel.id)
            if to_file_id in local_ids:
                score[to_file_id] += 0.25
                related_relation_ids[to_file_id].append(rel.id)
                boundary_file_ids.add(to_file_id)

        if rel.type == "calls" and from_file_id in local_ids and to_file_id in local_ids:
            calls_from_to[from_file_id].append((to_file_id, rel.id, rel.confidence))
            calls_to_from[to_file_id].append((from_file_id, rel.id, rel.confidence))

    contract_boosts: dict[str, float] = defaultdict(float)
    generator_files: set[str] = set()
    for contract in api_contracts:
        symbol_id = str(contract.get("symbol_id", ""))
        file_id = lookup.resolve_file_id(symbol_id)
        if file_id is None:
            continue
        kind = str(contract.get("kind", ""))
        if kind == "external_api_assumption":
            contract_boosts[file_id] += 0.8
        elif kind == "generator_protocol":
            contract_boosts[file_id] += 0.9
            generator_files.add(file_id)
        elif kind == "parameter_contract":
            contract_boosts[file_id] += 0.2

    for file_id, boost in contract_boosts.items():
        if file_id in local_ids:
            score[file_id] += boost

    for boundary_file_id in boundary_file_ids:
        for to_file_id, _, confidence in calls_from_to.get(boundary_file_id, []):
            inbound_from_boundary[to_file_id] += confidence + 0.25
            score[to_file_id] += 0.3
            direct_boundary_calls[to_file_id] += confidence

    for file_entry in local_files:
        if not _is_runtime_candidate(
            file_entry,
            boundary_file_ids=boundary_file_ids,
            contract_boosts=contract_boosts,
        ):
            continue
        for _, _, confidence in calls_from_to.get(file_entry.id, []):
            outbound_to_runtime[file_entry.id] += confidence

    runtime_candidates = sorted(
        (
            file_entry
            for file_entry in local_files
            if _is_runtime_candidate(
                file_entry,
                boundary_file_ids=boundary_file_ids,
                contract_boosts=contract_boosts,
            )
            and score.get(file_entry.id, 0.0) > 0
        ),
        key=lambda file_entry: (
            -_effective_runtime_score(
                file_entry,
                score,
                boundary_file_ids=boundary_file_ids,
                inbound_from_boundary=inbound_from_boundary,
                outbound_to_runtime=outbound_to_runtime,
                contract_boosts=contract_boosts,
                direct_boundary_calls=direct_boundary_calls,
                generator_files=generator_files,
            ),
            file_entry.path,
        ),
    )

    core_runtime_candidates = [
        file_entry
        for file_entry in runtime_candidates
        if not _is_support_runtime_file(file_entry) and file_entry.id not in boundary_file_ids
    ]
    boundary_runtime_candidates = [
        file_entry
        for file_entry in runtime_candidates
        if not _is_support_runtime_file(file_entry) and file_entry.id in boundary_file_ids
    ]
    support_runtime_candidates = [
        file_entry for file_entry in runtime_candidates if _is_support_runtime_file(file_entry)
    ]

    selected = core_runtime_candidates[:3]
    if len(selected) < 3:
        selected.extend(boundary_runtime_candidates[: 3 - len(selected)])
    if len(selected) < 3:
        selected.extend(support_runtime_candidates[: 3 - len(selected)])
    if not selected and boundary_runtime_candidates:
        selected.extend(boundary_runtime_candidates[:3])
    runtime_primary = [file_entry.id for file_entry in selected]
    runtime_primary_set = set(runtime_primary)

    runtime_secondary = sorted(
        {
            from_file_id
            for target_file_id in runtime_primary
            for from_file_id, _, _ in calls_to_from.get(target_file_id, [])
            if from_file_id in local_ids
            and from_file_id not in runtime_primary_set
            and from_file_id in boundary_file_ids
        }
    )

    change_targets: list[dict[str, object]] = []

    if runtime_primary:
        runtime_relation_ids: list[str] = []
        for file_id in runtime_primary:
            runtime_relation_ids.extend(related_relation_ids.get(file_id, []))
        change_targets.append(
            {
                "id": "change:runtime-core",
                "goal": "Identify the central runtime files to inspect first.",
                "priority": 1,
                "primary_files": runtime_primary,
                "secondary_files": runtime_secondary,
                "related_relations": sorted(set(runtime_relation_ids))[:12],
                "risks": [
                    "Core logic changes can cascade into UI and workflow paths.",
                    "External dependency handling may amplify regressions.",
                ],
            }
        )

    ui_primary = sorted(
        file_entry.id
        for file_entry in local_files
        if file_entry.id in boundary_file_ids
        and (
            file_entry.id in boundary_bind_files
            or any(to_file_id in runtime_primary_set for to_file_id, _, _ in calls_from_to.get(file_entry.id, []))
        )
    )
    if ui_primary:
        ui_related = sorted(
            {
                relation_id
                for file_id in ui_primary
                for relation_id in boundary_bind_relation_ids.get(file_id, [])
            }
            | {
                relation_id
                for file_id in ui_primary
                for to_file_id, relation_id, _ in calls_from_to.get(file_id, [])
                if to_file_id in runtime_primary_set
            }
        )
        change_targets.append(
            {
                "id": "change:entry-surface",
                "goal": "Inspect entry and boundary files that flow into runtime-core.",
                "priority": 2,
                "primary_files": ui_primary,
                "secondary_files": runtime_primary,
                "related_relations": ui_related[:12],
                "risks": [
                    "Entry surface changes can break handoff into core logic.",
                    "UI and route updates often require runtime adjustments too.",
                ],
            }
        )

    entry_config = sorted(
        file_entry.id
        for file_entry in local_files
        if file_entry.role in {"entrypoint_candidate", "configuration"}
        or file_entry.id in entrypoint_file_ids
    )
    if entry_config:
        change_targets.append(
            {
                "id": "change:entry-config",
                "goal": "Check bootstrap and configuration files that steer execution.",
                "priority": 3,
                "primary_files": entry_config,
                "secondary_files": runtime_primary,
                "related_relations": [],
                "risks": [
                    "Config drift can redirect runtime behavior broadly.",
                    "Bootstrap changes may impact startup and wiring.",
                ],
            }
        )

    subsystem_targets = _build_subsystem_targets(
        local_files=local_files,
        score=score,
        related_relation_ids=related_relation_ids,
        runtime_primary=runtime_primary,
        boundary_file_ids=boundary_file_ids,
    )
    change_targets.extend(subsystem_targets)
    return change_targets


def _build_subsystem_targets(
    *,
    local_files: list[FileEntry],
    score: dict[str, float],
    related_relation_ids: dict[str, list[str]],
    runtime_primary: list[str],
    boundary_file_ids: set[str],
) -> list[dict[str, object]]:
    if not local_files:
        return []

    runtime_set = set(runtime_primary)
    prefix_groups: dict[str, list[FileEntry]] = defaultdict(list)
    prefix_score: dict[str, float] = defaultdict(float)

    for file_entry in local_files:
        if file_entry.id in boundary_file_ids:
            continue
        if _is_support_runtime_file(file_entry):
            continue
        if score.get(file_entry.id, 0.0) <= 0:
            continue
        prefix = _subsystem_prefix(file_entry)
        if prefix is None:
            continue
        prefix_groups[prefix].append(file_entry)
        prefix_score[prefix] += score.get(file_entry.id, 0.0)

    ordered_prefixes = sorted(
        (
            prefix
            for prefix, members in prefix_groups.items()
            if len(members) >= 2 and prefix_score[prefix] > 1.0
        ),
        key=lambda prefix: (-prefix_score[prefix], -len(prefix_groups[prefix]), prefix),
    )

    targets: list[dict[str, object]] = []
    for priority, prefix in zip(count(4), ordered_prefixes[:3]):
        members = sorted(
            prefix_groups[prefix],
            key=lambda file_entry: (-score.get(file_entry.id, 0.0), file_entry.path),
        )
        primary_files = [file_entry.id for file_entry in members[:3]]
        primary_set = set(primary_files)
        secondary_files = sorted(
            {file_entry.id for file_entry in members if file_entry.id not in primary_set}
            | {file_id for file_id in runtime_set if file_id not in primary_set}
        )[:6]
        rel_ids: list[str] = []
        for file_id in primary_files:
            rel_ids.extend(related_relation_ids.get(file_id, []))
        targets.append(
            {
                "id": f"change:subsystem:{prefix}",
                "goal": f"{prefix} subsystem start point for modification bootstrap",
                "priority": priority,
                "primary_files": primary_files,
                "secondary_files": secondary_files,
                "related_relations": sorted(set(rel_ids))[:12],
                "risks": [
                    "Cross-subsystem dependency changes may spread beyond the local package.",
                    "Entry surface and runtime core can still interact across subsystem boundaries.",
                ],
            }
        )
    return targets


def _subsystem_prefix(file_entry: FileEntry) -> str | None:
    parts = [part for part in file_entry.module.split(".") if part]
    if len(parts) >= 2:
        return ".".join(parts[:2])
    if parts:
        return parts[0]
    return None


def _effective_runtime_score(
    file_entry: FileEntry,
    score: dict[str, float],
    *,
    boundary_file_ids: set[str],
    inbound_from_boundary: dict[str, float],
    outbound_to_runtime: dict[str, float],
    contract_boosts: dict[str, float],
    direct_boundary_calls: dict[str, float],
    generator_files: set[str],
) -> float:
    base = score.get(file_entry.id, 0.0)
    base += inbound_from_boundary.get(file_entry.id, 0.0)
    base += outbound_to_runtime.get(file_entry.id, 0.0) * 0.15
    base += contract_boosts.get(file_entry.id, 0.0) * 0.4
    base += direct_boundary_calls.get(file_entry.id, 0.0) * 0.8
    if file_entry.id in generator_files and direct_boundary_calls.get(file_entry.id, 0.0) > 0:
        base += 4.0
    if file_entry.id in boundary_file_ids:
        base -= 0.75
    base -= _utility_penalty(file_entry.path)
    return base


def _utility_penalty(path: str) -> float:
    name = path.rsplit("/", 1)[-1].lower()
    penalty = 0.0
    if name in {"__init__.py", "utils.py", "helpers.py", "common.py"}:
        penalty += 1.0
    if "util" in name or "helper" in name:
        penalty += 0.4
    return penalty


def _is_runtime_candidate(
    file_entry: FileEntry,
    *,
    boundary_file_ids: set[str] | None = None,
    contract_boosts: dict[str, float] | None = None,
) -> bool:
    if file_entry.role not in {"source", "entrypoint_candidate"}:
        return False
    path = file_entry.path
    name = path.rsplit("/", 1)[-1]
    if path == "config.py" or name == "__init__.py":
        return False
    return path.endswith(".py")


def _is_support_runtime_file(file_entry: FileEntry) -> bool:
    name = file_entry.path.rsplit("/", 1)[-1].lower()
    return name in {"utils.py", "helpers.py", "common.py"} or "util" in name or "helper" in name


def default_impact_rules() -> list[dict[str, str]]:
    return [
        {
            "id": "impact:calls-reverse",
            "trigger_relation": "calls",
            "direction": "reverse",
            "description": "Trace callers when a callee changes.",
        },
        {
            "id": "impact:imports-reverse",
            "trigger_relation": "imports",
            "direction": "reverse",
            "description": "Trace importers when an imported file changes.",
        },
        {
            "id": "impact:cluster-neighbor",
            "trigger_relation": "cluster",
            "direction": "within_cluster",
            "description": "Inspect neighboring files in the same cluster.",
        },
    ]
