from __future__ import annotations

from collections import defaultdict
from itertools import count

from .model import Entrypoint, FileEntry, Relation


def build_change_targets(
    files: list[FileEntry],
    relations: list[Relation],
    entrypoints: list[Entrypoint] | None = None,
    api_contracts: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    file_map = {f.id: f for f in files}
    local_files = [f for f in files if f.id.startswith("file:")]
    local_ids = set(file_map)
    entrypoints = entrypoints or []
    api_contracts = api_contracts or []
    entrypoint_file_ids = {
        symbol_id
        for symbol_id in (
            _symbol_to_file_id(e.symbol_id, file_map, local_files) for e in entrypoints
        )
        if symbol_id is not None
    }

    score: dict[str, float] = defaultdict(float)
    related_relation_ids: dict[str, list[str]] = defaultdict(list)
    boundary_file_ids = set(entrypoint_file_ids)
    inbound_from_boundary: dict[str, float] = defaultdict(float)
    outbound_to_runtime: dict[str, float] = defaultdict(float)
    direct_boundary_calls: dict[str, float] = defaultdict(float)

    for rel in relations:
        if rel.type not in {"calls", "imports", "inherits"}:
            continue
        from_file_id = _symbol_to_file_id(rel.from_id, file_map, local_files)
        to_file_id = _symbol_to_file_id(rel.to_id, file_map, local_files)
        if from_file_id not in local_ids or to_file_id not in local_ids:
            continue
        score[to_file_id] += rel.confidence
        related_relation_ids[to_file_id].append(rel.id)

    # UI event bindings are especially important for modification bootstrap:
    # they indicate that a file is on an interaction boundary.
    for rel in relations:
        if rel.type not in {"ui_binds", "route_binds", "command_binds"}:
            continue
        from_file_id = _symbol_to_file_id(rel.from_id, file_map, local_files)
        if from_file_id in local_ids:
            score[from_file_id] += 0.35
            boundary_file_ids.add(from_file_id)
        target_file_id = _symbol_to_file_id(rel.to_id, file_map, local_files)
        if target_file_id is not None:
            score[target_file_id] += 0.25
            related_relation_ids[target_file_id].append(rel.id)
            boundary_file_ids.add(target_file_id)

    for rel in relations:
        if rel.type != "calls":
            continue
        from_file_id = _symbol_to_file_id(rel.from_id, file_map, local_files)
        to_file_id = _symbol_to_file_id(rel.to_id, file_map, local_files)
        if from_file_id not in local_ids or to_file_id not in local_ids:
            continue
        if from_file_id in boundary_file_ids:
            inbound_from_boundary[to_file_id] += rel.confidence + 0.25
            score[to_file_id] += 0.3
            direct_boundary_calls[to_file_id] += rel.confidence

    # External API assumptions are strong signals that a file is likely to be
    # a real modification hotspot rather than a passive utility.
    contract_boosts: dict[str, float] = defaultdict(float)
    generator_files: set[str] = set()
    for contract in api_contracts:
        symbol_id = str(contract.get("symbol_id", ""))
        file_id = _symbol_to_file_id(symbol_id, file_map, local_files)
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

    for rel in relations:
        if rel.type != "calls":
            continue
        from_file_id = _symbol_to_file_id(rel.from_id, file_map, local_files)
        to_file_id = _symbol_to_file_id(rel.to_id, file_map, local_files)
        if from_file_id not in local_ids or to_file_id not in local_ids:
            continue
        if _is_runtime_candidate(
            file_map[from_file_id],
            boundary_file_ids=boundary_file_ids,
            contract_boosts=contract_boosts,
        ):
            outbound_to_runtime[from_file_id] += rel.confidence

    runtime_candidates = sorted(
        (
            f
            for f in local_files
            if _is_runtime_candidate(
                f,
                boundary_file_ids=boundary_file_ids,
                contract_boosts=contract_boosts,
            )
            and score.get(f.id, 0.0) > 0
        ),
        key=lambda f: (
            -_effective_runtime_score(
                f,
                score,
                boundary_file_ids=boundary_file_ids,
                inbound_from_boundary=inbound_from_boundary,
                outbound_to_runtime=outbound_to_runtime,
                contract_boosts=contract_boosts,
                direct_boundary_calls=direct_boundary_calls,
                generator_files=generator_files,
            ),
            f.path,
        ),
    )
    core_runtime_candidates = [
        f
        for f in runtime_candidates
        if not _is_support_runtime_file(f) and f.id not in boundary_file_ids
    ]
    boundary_runtime_candidates = [
        f
        for f in runtime_candidates
        if not _is_support_runtime_file(f) and f.id in boundary_file_ids
    ]
    support_runtime_candidates = [f for f in runtime_candidates if _is_support_runtime_file(f)]
    selected = core_runtime_candidates[:3]
    if len(selected) < 3:
        selected.extend(boundary_runtime_candidates[: 3 - len(selected)])
    if len(selected) < 3:
        selected.extend(support_runtime_candidates[: 3 - len(selected)])
    if not selected and boundary_runtime_candidates:
        selected.extend(boundary_runtime_candidates[:3])
    runtime_primary = [f.id for f in selected]

    runtime_secondary = sorted(
        {
            _symbol_to_file_id(rel.from_id, file_map, local_files)
            for rel in relations
            if _symbol_to_file_id(rel.to_id, file_map, local_files) in set(runtime_primary)
            and _symbol_to_file_id(rel.from_id, file_map, local_files) in local_ids
            and _symbol_to_file_id(rel.from_id, file_map, local_files) not in set(runtime_primary)
            and _symbol_to_file_id(rel.from_id, file_map, local_files) in boundary_file_ids
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
                "goal": "中核ロジックの改造開始点を特定する",
                "priority": 1,
                "primary_files": runtime_primary,
                "secondary_files": runtime_secondary,
                "related_relations": sorted(set(runtime_relation_ids))[:12],
                "risks": [
                    "中核ロジック変更が UI や他モジュールへ波及する可能性",
                    "外部ライブラリ依存の契約差分",
                ],
            }
        )

    ui_primary = sorted(
        f.id
        for f in local_files
        if f.id in boundary_file_ids
        and any(
            (
                _symbol_to_file_id(rel.from_id, file_map, local_files) == f.id
                and _symbol_to_file_id(rel.to_id, file_map, local_files) in set(runtime_primary)
            ) or (
                rel.type in {"ui_binds", "route_binds", "command_binds"}
                and _symbol_to_file_id(rel.from_id, file_map, local_files) == f.id
            )
            for rel in relations
        )
    )
    if ui_primary:
        ui_related = [
            rel.id
            for rel in relations
            if _symbol_to_file_id(rel.from_id, file_map, local_files) in set(ui_primary)
            and (
                _symbol_to_file_id(rel.to_id, file_map, local_files) in set(runtime_primary)
                or rel.type in {"ui_binds", "route_binds", "command_binds"}
            )
        ]
        change_targets.append(
            {
                "id": "change:entry-surface",
                "goal": "入口層と中核ロジックの結合点を確認する",
                "priority": 2,
                "primary_files": ui_primary,
                "secondary_files": runtime_primary,
                "related_relations": sorted(set(ui_related))[:12],
                "risks": [
                    "入口層と実処理層の契約不整合",
                    "導線の見落とし",
                ],
            }
        )

    entry_config = sorted(
        f.id
        for f in local_files
        if f.role in {"entrypoint_candidate", "configuration"} or f.id in entrypoint_file_ids
    )
    if entry_config:
        change_targets.append(
            {
                "id": "change:entry-config",
                "goal": "起動点と設定値の影響範囲を確認する",
                "priority": 3,
                "primary_files": entry_config,
                "secondary_files": runtime_primary,
                "related_relations": [],
                "risks": [
                    "起動導線の崩れ",
                    "設定値変更による広域影響",
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
        key=lambda prefix: (
            -prefix_score[prefix],
            -len(prefix_groups[prefix]),
            prefix,
        ),
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
            {
                file_entry.id
                for file_entry in members
                if file_entry.id not in primary_set
            }
            | {
                file_id
                for file_id in runtime_set
                if file_id not in primary_set
            }
        )[:6]
        rel_ids: list[str] = []
        for file_id in primary_files:
            rel_ids.extend(related_relation_ids.get(file_id, []))
        targets.append(
            {
                'id': f'change:subsystem:{prefix}',
                'goal': f'{prefix} subsystem start point for modification bootstrap',
                'priority': priority,
                'primary_files': primary_files,
                'secondary_files': secondary_files,
                'related_relations': sorted(set(rel_ids))[:12],
                'risks': [
                    'cross-subsystem dependency changes may spread beyond the local package',
                    'entry surface and runtime core can still interact across subsystem boundaries',
                ],
            }
        )
    return targets


def _subsystem_prefix(file_entry: FileEntry) -> str | None:
    parts = [part for part in file_entry.module.split('.') if part]
    if len(parts) >= 2:
        return '.'.join(parts[:2])
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


def _symbol_to_file_id(
    symbol_or_file_id: str,
    file_map: dict[str, FileEntry],
    local_files: list[FileEntry],
) -> str | None:
    if symbol_or_file_id in file_map:
        return symbol_or_file_id
    candidates: list[tuple[int, str]] = []
    for file_entry in local_files:
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


def default_impact_rules() -> list[dict[str, str]]:
    return [
        {
            "id": "impact:calls-reverse",
            "trigger_relation": "calls",
            "direction": "reverse",
            "description": "callee を変更した場合、caller を確認対象にする",
        },
        {
            "id": "impact:imports-reverse",
            "trigger_relation": "imports",
            "direction": "reverse",
            "description": "import 先変更時は import 元を確認対象にする",
        },
        {
            "id": "impact:cluster-neighbor",
            "trigger_relation": "cluster",
            "direction": "within_cluster",
            "description": "同一責務クラスタ内のファイルを確認対象にする",
        },
    ]
