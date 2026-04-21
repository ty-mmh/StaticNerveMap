from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any

import yaml

from . import SCHEMA_VERSION, SNAPSHOT_SCHEMA_VERSION, __version__
from .api_contracts import build_api_contracts
from .change_targets import build_change_targets, default_impact_rules
from .clusters import build_clusters
from .model import AnalysisResult, Entrypoint, FileEntry, Note, Project, Relation, Unresolved
from .modification_paths import build_modification_paths
from .postprocess_lookup import build_file_lookup, resolve_relation_file_ids
from .python_extractor import (
    collect_public_exports_for_file,
    finalize_param_type_hints,
    collect_relations_for_file,
    collect_symbols_for_file,
    parse_file,
)
from .resolver import Resolver
from .scanner import _choose_primary_packages, compute_excluded_dirs, scan_repo
from .unresolved_compression import compress_unresolved
from .yaml_writer import dump_yaml


def build_analysis_meta(project_name: str) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "analyzer_version": __version__,
        "strategy": {
            "parser": "python-ast",
            "llm_used": False,
            "mcp_used": False,
        },
        "focus": {
            "primary_goal": "modification_bootstrap",
            "intended_agent": ["Codex", "Claude Code"],
        },
    }


def _select_inference_budget(scan_mode: str, parsed_file_count: int) -> tuple[int, str]:
    if scan_mode == "focused":
        return 1, "focused_light"
    if parsed_file_count >= 5000:
        return 1, "large_repo_light"
    if parsed_file_count >= 1500:
        return 2, "medium_repo_reduced"
    return 3, "default_full"


def _select_postprocess_budget(scan_mode: str, relation_count: int) -> tuple[int | None, str]:
    if relation_count < 10000:
        return None, "full_relations"
    if scan_mode == "focused":
        return 12000, "focused_budgeted"
    if relation_count >= 50000:
        return 16000, "large_repo_budgeted"
    return 24000, "default_budgeted"


def _budget_postprocess_relations(
    relations: list[Relation],
    files: list[FileEntry],
    entrypoints: list[Entrypoint],
    max_relations: int | None,
) -> tuple[list[Relation], bool]:
    if max_relations is None or len(relations) <= max_relations:
        return relations, False

    lookup = build_file_lookup(files)
    relation_counts: dict[str, int] = {}
    hot_files: set[str] = set()

    for entry in entrypoints:
        file_id = lookup.resolve_file_id(entry.symbol_id)
        if file_id is not None:
            hot_files.add(file_id)

    for file_entry in lookup.local_files:
        if file_entry.role in {"entrypoint_candidate", "configuration"}:
            hot_files.add(file_entry.id)

    resolved_relations = resolve_relation_file_ids(relations, lookup)
    for _, from_file, to_file in resolved_relations:
        if from_file is not None:
            relation_counts[from_file] = relation_counts.get(from_file, 0) + 1
        if to_file is not None:
            relation_counts[to_file] = relation_counts.get(to_file, 0) + 1

    for file_id, _ in sorted(relation_counts.items(), key=lambda item: (-item[1], item[0]))[:200]:
        hot_files.add(file_id)

    prioritized: list[tuple[int, int, Relation]] = []
    for idx, (rel, from_file, to_file) in enumerate(resolved_relations):
        touches_hot = from_file in hot_files or to_file in hot_files
        if rel.type in {"imports", "ui_binds", "route_binds", "command_binds", "inherits"}:
            priority = 0
        elif touches_hot:
            priority = 1
        elif rel.type == "calls":
            priority = 2
        else:
            priority = 3
        prioritized.append((priority, idx, rel))

    prioritized.sort(key=lambda item: (item[0], item[1]))
    kept = [rel for _, _, rel in prioritized[:max_relations]]
    kept.sort(key=lambda rel: rel.id)
    return kept, True


def analyze(repo_root: Path, project_name: str, scan_mode: str = "default") -> AnalysisResult:
    t0 = time.perf_counter()
    files = scan_repo(repo_root, scan_mode=scan_mode)
    t1 = time.perf_counter()

    # phase 1: parse all, collect symbols
    parsed: list[tuple] = []  # list of (file_entry, tree)
    all_symbols = []
    for f in files:
        tree = parse_file(repo_root / f.path)
        if tree is None:
            continue
        parsed.append((f, tree))
        all_symbols.extend(collect_symbols_for_file(tree, f))
    t2 = time.perf_counter()

    # phase 2: build resolver, collect relations
    resolver = Resolver(files=files, symbols=all_symbols)
    symbol_by_id = {symbol.id: symbol for symbol in all_symbols}
    for file_entry, tree in parsed:
        for symbol_id in collect_public_exports_for_file(tree, file_entry, resolver):
            symbol = symbol_by_id.get(symbol_id)
            if symbol is None:
                continue
            symbol.is_public_api = True
            exported_via = list(symbol.exported_via or [])
            if file_entry.id not in exported_via:
                exported_via.append(file_entry.id)
            symbol.exported_via = exported_via

    inference_rounds_max, inference_mode = _select_inference_budget(scan_mode, len(parsed))
    infer_rounds_used = 0
    inferred_param_types: dict[str, dict[str, str]] = {}
    for _ in range(inference_rounds_max):
        infer_rounds_used += 1
        resolver.set_inferred_param_types(inferred_param_types)
        hint_states = []
        for file_entry, tree in parsed:
            state = collect_relations_for_file(tree, file_entry, resolver)
            hint_states.append(state)
        next_param_types = dict(inferred_param_types)
        for symbol_id, params in finalize_param_type_hints(hint_states).items():
            by_symbol = next_param_types.setdefault(symbol_id, {})
            by_symbol.update(params)
        if next_param_types == inferred_param_types:
            break
        inferred_param_types = next_param_types

    resolver.set_inferred_param_types(inferred_param_types)
    t3 = time.perf_counter()

    all_relations = []
    all_entrypoints = []
    all_unresolved: list[Unresolved] = []
    all_notes: list[Note] = []
    for file_entry, tree in parsed:
        state = collect_relations_for_file(tree, file_entry, resolver)
        all_relations.extend(state.relations)
        all_entrypoints.extend(state.entrypoints)
        all_unresolved.extend(state.unresolved)
        all_notes.extend(state.notes)
    t4 = time.perf_counter()

    # synthetic "external" file entries for external modules referenced
    external_file_entries = [
        _external_file_entry(mod) for mod in sorted(resolver.external_modules)
    ]
    all_files = list(files) + external_file_entries

    project = Project(
        name=project_name,
        root_path=str(repo_root).replace("\\", "/"),
        languages=["python"],
        primary_language="python",
    )

    effective_excluded_dirs = compute_excluded_dirs(repo_root, scan_mode=scan_mode)
    analysis_meta = build_analysis_meta(project_name)
    scope_meta: dict[str, Any] = {
        "included": ["**/*.py"],
        "excluded": [f"{name}/" for name in sorted(effective_excluded_dirs)],
        "scan_mode": scan_mode,
    }
    if scan_mode == "focused":
        scope_meta["primary_packages"] = sorted(_choose_primary_packages(files))
    analysis_meta["scope"] = scope_meta

    postprocess_relation_budget, postprocess_mode = _select_postprocess_budget(
        scan_mode,
        len(all_relations),
    )
    postprocess_relations, postprocess_budget_applied = _budget_postprocess_relations(
        all_relations,
        all_files,
        all_entrypoints,
        postprocess_relation_budget,
    )

    p0 = time.perf_counter()
    api_contracts = build_api_contracts(all_files, all_symbols, postprocess_relations)
    p1 = time.perf_counter()
    change_targets = build_change_targets(
        files,
        postprocess_relations,
        entrypoints=all_entrypoints,
        api_contracts=api_contracts,
    )
    p2 = time.perf_counter()
    modification_paths = build_modification_paths(
        postprocess_relations,
        all_symbols,
        entrypoints=all_entrypoints,
        change_targets=change_targets,
    )
    p3 = time.perf_counter()
    clusters = build_clusters(all_files, postprocess_relations, entrypoints=all_entrypoints)
    p4 = time.perf_counter()
    all_unresolved = compress_unresolved(
        all_unresolved,
        files=all_files,
        symbols=all_symbols,
    )
    p5 = time.perf_counter()
    t5 = time.perf_counter()

    analysis_meta["profiling"] = {
        "scan_seconds": round(t1 - t0, 6),
        "parse_symbols_seconds": round(t2 - t1, 6),
        "infer_seconds": round(t3 - t2, 6),
        "relations_seconds": round(t4 - t3, 6),
        "postprocess_seconds": round(t5 - t4, 6),
        "total_seconds": round(t5 - t0, 6),
        "focused_mode": scan_mode == "focused",
        "inference_mode": inference_mode,
        "infer_rounds_max": inference_rounds_max,
        "infer_rounds_used": infer_rounds_used,
        "postprocess_mode": postprocess_mode,
        "postprocess_budget_applied": postprocess_budget_applied,
        "postprocess_relations_input": len(postprocess_relations),
        "api_contracts_seconds": round(p1 - p0, 6),
        "change_targets_seconds": round(p2 - p1, 6),
        "modification_paths_seconds": round(p3 - p2, 6),
        "clusters_seconds": round(p4 - p3, 6),
        "unresolved_compression_seconds": round(p5 - p4, 6),
    }
    analysis_meta["counts"] = {
        "scanned_files": len(files),
        "parsed_files": len(parsed),
        "symbol_count": len(all_symbols),
        "relation_count": len(all_relations),
        "entrypoint_count": len(all_entrypoints),
        "unresolved_count": len(all_unresolved),
    }

    return AnalysisResult(
        project=project,
        analysis=analysis_meta,
        schema_version=SCHEMA_VERSION,
        files=all_files,
        symbols=all_symbols,
        relations=all_relations,
        entrypoints=all_entrypoints,
        clusters=clusters,
        change_targets=change_targets,
        modification_paths=modification_paths,
        impact_rules=default_impact_rules(),
        api_contracts=api_contracts,
        unresolved=all_unresolved,
        notes=all_notes,
    )


def _detect_git_context(repo_root: Path) -> tuple[str | None, str, bool]:
    git_dir = repo_root / ".git"
    if not git_dir.exists():
        return None, "", False

    def _run(*args: str) -> str | None:
        try:
            proc = subprocess.run(
                args,
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError):
            return None
        return proc.stdout.strip()

    commit_hash = _run("git", "rev-parse", "HEAD")
    branch = _run("git", "rev-parse", "--abbrev-ref", "HEAD") or ""
    status = _run("git", "status", "--porcelain")
    git_dirty = bool(status)
    return commit_hash, branch, git_dirty


def _parse_snapshot_id(snapshot_id: str) -> tuple[str | None, str]:
    match = re.match(r"^(?P<prefix>[A-Za-z0-9]+)-(?P<stage>[a-z_]+)-(?P<seq>\d+)$", snapshot_id)
    if not match:
        return None, "post"
    prefix = match.group("prefix")
    stage = match.group("stage")
    milestone_id = prefix if prefix.startswith("M") else None
    return milestone_id, stage


def _infer_snapshot_kind(stage: str) -> str:
    if stage == "baseline":
        return "baseline"
    if stage == "pre":
        return "pre_milestone"
    if stage == "post":
        return "post_milestone"
    return "checkpoint"


def _default_snapshot_output(repo_root: Path, snapshot_id: str) -> Path:
    return repo_root / "static-nervemap" / "snapshots" / f"{snapshot_id}.yaml"


def _ensure_layered_dirs(base_dir: Path) -> None:
    base_dir.mkdir(parents=True, exist_ok=True)
    (base_dir / "snapshots").mkdir(parents=True, exist_ok=True)
    (base_dir / "deltas").mkdir(parents=True, exist_ok=True)


def _next_snapshot_sequence(snapshot_dir: Path) -> int:
    if not snapshot_dir.exists():
        return 0
    max_sequence = -1
    for path in snapshot_dir.glob("*.yaml"):
        try:
            doc = _load_yaml(path)
        except Exception:
            continue
        snapshot = doc.get("snapshot")
        if not isinstance(snapshot, dict):
            continue
        try:
            sequence = int(snapshot.get("sequence", -1))
        except (TypeError, ValueError):
            continue
        max_sequence = max(max_sequence, sequence)
    return max_sequence + 1


def _infer_milestone_from_roadmap_ref(roadmap_ref: str | None) -> str | None:
    if not roadmap_ref:
        return None
    match = re.search(r"#(?:phase|task)-(?P<phase>\d+)(?:-\d+)?$", roadmap_ref)
    if match:
        return f"M{int(match.group('phase')):02d}"
    return None


def _resolve_roadmap_path(repo_root: Path, roadmap_ref: str | None) -> Path | None:
    if not roadmap_ref:
        return None
    ref_path = roadmap_ref.split("#", 1)[0].strip()
    if not ref_path:
        return None
    candidates = [
        repo_root / ref_path,
        Path.cwd() / ref_path,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _infer_milestone_title(repo_root: Path, roadmap_ref: str | None, milestone_id: str | None) -> str | None:
    roadmap_path = _resolve_roadmap_path(repo_root, roadmap_ref)
    if roadmap_path is None or milestone_id is None:
        return None
    try:
        text = roadmap_path.read_text(encoding="utf-8")
    except OSError:
        return None

    phase_num = None
    milestone_match = re.match(r"^M(\d+)$", milestone_id)
    if milestone_match:
        phase_num = int(milestone_match.group(1))

    if phase_num is None and roadmap_ref:
        ref_match = re.search(r"#(?:phase|task)-(?P<phase>\d+)(?:-\d+)?$", roadmap_ref)
        if ref_match:
            phase_num = int(ref_match.group("phase"))
    if phase_num is None:
        return None

    heading_patterns = [
        rf"^##\s*フェーズ{phase_num}\s*:\s*(?P<title>.+?)\s*$",
        rf"^##\s*Phase\s+{phase_num}\s*:\s*(?P<title>.+?)\s*$",
        rf"^##\s*M0*{phase_num}\s*[:\-]\s*(?P<title>.+?)\s*$",
    ]
    for pattern in heading_patterns:
        phase_match = re.search(pattern, text, re.MULTILINE)
        if phase_match:
            return phase_match.group("title").strip()
    return None


def _build_snapshot_tags(stage: str, milestone_id: str | None) -> list[str]:
    tags = ["snapshot", stage]
    if milestone_id:
        tags.append(milestone_id)
    return tags


def _build_repo_fingerprint(result: AnalysisResult) -> dict[str, int]:
    return {
        "file_count": len(result.files),
        "symbol_count": len(result.symbols),
        "relation_count": len(result.relations),
        "unresolved_count": len(result.unresolved),
    }


UNRESOLVED_HEAVY_ABSOLUTE_FLOOR = 10
UNRESOLVED_HEAVY_RATIO_THRESHOLD = 0.05


def _is_unresolved_heavy(result: AnalysisResult) -> bool:
    medium_count = sum(1 for u in result.unresolved if u.severity == "medium")
    if medium_count < UNRESOLVED_HEAVY_ABSOLUTE_FLOOR:
        return False
    symbol_count = len(result.symbols)
    ratio = medium_count / max(symbol_count, 1)
    return ratio >= UNRESOLVED_HEAVY_RATIO_THRESHOLD


def _build_snapshot_summary(result: AnalysisResult) -> dict[str, Any]:
    top_change_targets = [
        target.get("id")
        for target in result.change_targets[:3]
        if isinstance(target, dict) and target.get("id")
    ]
    top_clusters = [
        cluster.get("id")
        for cluster in result.clusters[:3]
        if isinstance(cluster, dict) and cluster.get("id")
    ]
    risk_summary: list[str] = []
    medium_unresolved = [u for u in result.unresolved if u.severity == "medium"]
    low_unresolved = [u for u in result.unresolved if u.severity == "low"]
    if top_change_targets:
        risk_summary.append(
            f"改造の主対象候補は {top_change_targets[0]} を中心に読むのが自然。"
        )
    if medium_unresolved:
        risk_summary.append(
            f"未解決 call が {len(medium_unresolved)} 件あり、追加調査が必要な箇所が残る。"
        )
    elif low_unresolved:
        risk_summary.append(
            f"未解決項目は {len(low_unresolved)} 件だが、低信号なものが中心。"
        )
    if result.modification_paths:
        risk_summary.append(
            f"UI から core への改造導線を {len(result.modification_paths)} 本保持している。"
        )
    elif top_change_targets:
        risk_summary.append(
            "入口から core までの導線は弱めなので、change target を起点に追うのが安全。"
        )
    if not risk_summary:
        risk_summary.append(
            "大きな未解決や導線不足は目立たず、主要 change target から読み始めやすい。"
        )
    return {
        "top_change_targets": top_change_targets,
        "top_clusters": top_clusters,
        "risk_summary": risk_summary,
    }


def _merge_index_summary(doc: dict[str, Any], snap: dict[str, Any]) -> dict[str, Any]:
    snapshot_summary = snap.get("summary") if isinstance(snap.get("summary"), dict) else {}
    repo_fingerprint = snap.get("repo_fingerprint") if isinstance(snap.get("repo_fingerprint"), dict) else {}
    summary = {
        "file_count": repo_fingerprint.get("file_count", len(doc.get("files", []))),
        "symbol_count": repo_fingerprint.get("symbol_count", len(doc.get("symbols", []))),
        "relation_count": repo_fingerprint.get("relation_count", len(doc.get("relations", []))),
        "unresolved_count": repo_fingerprint.get("unresolved_count", len(doc.get("unresolved", []))),
        "top_change_targets": snapshot_summary.get("top_change_targets")
        or [t.get("id") for t in doc.get("change_targets", [])[:3] if isinstance(t, dict)],
        "top_clusters": snapshot_summary.get("top_clusters")
        or [c.get("id") for c in doc.get("clusters", [])[:3] if isinstance(c, dict)],
    }
    risk_summary = snapshot_summary.get("risk_summary")
    if isinstance(risk_summary, list) and risk_summary:
        summary["risk_summary"] = risk_summary
    return summary


def _load_existing_snapshots(snapshot_dir: Path) -> list[dict[str, Any]]:
    if not snapshot_dir.exists():
        return []
    loaded: list[dict[str, Any]] = []
    for path in snapshot_dir.glob("*.yaml"):
        try:
            doc = _load_yaml(path)
        except Exception:
            continue
        snapshot = doc.get("snapshot")
        if isinstance(snapshot, dict):
            loaded.append(snapshot)
    return loaded


def _snapshot_stage_rank(stage: str | None) -> int:
    order = {"baseline": 0, "pre": 1, "mid": 2, "post": 3}
    return order.get(stage or "", 99)


def _infer_parent_snapshot_id(
    existing_snapshots: list[dict[str, Any]],
    milestone_id: str | None,
    stage: str,
) -> str | None:
    if not existing_snapshots:
        return None

    same_milestone = [
        snap for snap in existing_snapshots
        if snap.get("milestone_id") == milestone_id
    ]
    if same_milestone and stage in {"pre", "mid", "post"}:
        lower_stage = [
            snap for snap in same_milestone
            if _snapshot_stage_rank(snap.get("stage")) < _snapshot_stage_rank(stage)
        ]
        if lower_stage:
            lower_stage.sort(key=lambda snap: int(snap.get("sequence", -1)))
            return lower_stage[-1].get("snapshot_id")

    existing_snapshots = sorted(
        existing_snapshots,
        key=lambda snap: int(snap.get("sequence", -1)),
    )
    return existing_snapshots[-1].get("snapshot_id")


def _snapshot_id_parts(snapshot_id: str) -> tuple[str | None, str | None, int | None]:
    match = re.match(r"^(?P<prefix>[A-Za-z0-9]+)-(?P<stage>[a-z_]+)-(?P<seq>\d+)$", snapshot_id)
    if not match:
        return None, None, None
    return match.group("prefix"), match.group("stage"), int(match.group("seq"))


def suggest_snapshot_id(
    repo_root: Path,
    roadmap_ref: str | None = None,
    stage: str | None = None,
    snapshot_dir: Path | None = None,
) -> str:
    snapshot_dir = snapshot_dir or (repo_root / "static-nervemap" / "snapshots")
    milestone_id = _infer_milestone_from_roadmap_ref(roadmap_ref)
    prefix = milestone_id or "GEN"
    stage_value = stage or "post"
    max_n = 0
    for snap in _load_existing_snapshots(snapshot_dir):
        snap_id = snap.get("snapshot_id")
        if not isinstance(snap_id, str):
            continue
        existing_prefix, existing_stage, existing_n = _snapshot_id_parts(snap_id)
        if existing_prefix == prefix and existing_stage == stage_value and existing_n is not None:
            max_n = max(max_n, existing_n)
    return f"{prefix}-{stage_value}-{max_n + 1:03d}"


def create_snapshot(
    repo_root: Path,
    project_name: str,
    snapshot_id: str,
    out_path: Path | None = None,
    parent_snapshot_id: str | None = None,
    milestone_id: str | None = None,
    stage: str | None = None,
    kind: str | None = None,
    roadmap_ref: str | None = None,
    change_reason: str = "",
    scope_note: str = "",
    scan_mode: str = "default",
) -> tuple[AnalysisResult, Path]:
    result = analyze(repo_root, project_name, scan_mode=scan_mode)
    if out_path is not None:
        snapshot_dir = out_path.parent
        if snapshot_dir.name == "snapshots":
            _ensure_layered_dirs(snapshot_dir.parent)
        else:
            snapshot_dir.mkdir(parents=True, exist_ok=True)
    else:
        layered_root = repo_root / "static-nervemap"
        snapshot_dir = layered_root / "snapshots"
        _ensure_layered_dirs(layered_root)
    existing_snapshots = [
        snap for snap in _load_existing_snapshots(snapshot_dir)
        if snap.get("snapshot_id") != snapshot_id
    ]
    sequence = _next_snapshot_sequence(snapshot_dir)
    inferred_milestone_id, inferred_stage = _parse_snapshot_id(snapshot_id)
    stage_value = stage or inferred_stage
    kind_value = kind or _infer_snapshot_kind(stage_value)
    roadmap_milestone_id = _infer_milestone_from_roadmap_ref(roadmap_ref)
    milestone_value = milestone_id or inferred_milestone_id or roadmap_milestone_id
    parent_value = parent_snapshot_id or _infer_parent_snapshot_id(
        existing_snapshots,
        milestone_id=milestone_value,
        stage=stage_value,
    )
    commit_hash, branch, git_dirty = _detect_git_context(repo_root)
    stable = (
        (not git_dirty)
        and stage_value in {"post", "baseline"}
        and not _is_unresolved_heavy(result)
    )

    phase_ref = roadmap_ref
    if milestone_value and milestone_value.startswith("M") and milestone_value[1:].isdigit():
        phase_ref = phase_ref or f"docs/ImplementationRoadmap.md#phase-{int(milestone_value[1:])}"

    milestone_title = _infer_milestone_title(repo_root, phase_ref, milestone_value)
    repo_fingerprint = _build_repo_fingerprint(result)
    summary = _build_snapshot_summary(result)
    tags = _build_snapshot_tags(stage_value, milestone_value)

    generated_at = result.analysis["generated_at"]
    result.schema_version = SNAPSHOT_SCHEMA_VERSION
    result.snapshot = {
        "snapshot_id": snapshot_id,
        "parent_snapshot_id": parent_value,
        "sequence": sequence,
        "kind": kind_value,
        "stage": stage_value,
        "milestone_id": milestone_value,
        "milestone_title": milestone_title,
        "roadmap_ref": phase_ref,
        "generated_at": generated_at,
        "commit_hash": commit_hash,
        "git_dirty": git_dirty,
        "stable": stable,
        "branch": branch,
        "change_reason": change_reason,
        "scope_note": scope_note,
        "tags": tags,
        "repo_fingerprint": repo_fingerprint,
        "summary": summary,
    }
    final_out = out_path or _default_snapshot_output(repo_root, snapshot_id)
    return result, final_out


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"invalid yaml object: {path}")
    return data


def _milestone_sort_key(milestone_id: str) -> tuple[int, str]:
    match = re.match(r"^M(\d+)$", milestone_id)
    if match:
        return int(match.group(1)), milestone_id
    return 10**9, milestone_id


def _default_index_output(snapshot_dir: Path) -> Path:
    return snapshot_dir.parent / "index.yaml"


def rebuild_index(snapshot_dir: Path, out_path: Path | None = None) -> tuple[dict[str, Any], Path]:
    snapshot_files = sorted(snapshot_dir.glob("*.yaml"))
    if not snapshot_files:
        raise ValueError(
            f"no snapshot yaml found in {snapshot_dir} (expected files under static-nervemap/snapshots)"
        )
    loaded = [(path, _load_yaml(path)) for path in snapshot_files]
    loaded = [(path, doc) for path, doc in loaded if isinstance(doc.get("snapshot"), dict)]
    loaded = sorted(loaded, key=lambda item: int(item[1]["snapshot"].get("sequence", 0)))
    snapshots = [doc for _, doc in loaded]
    if not snapshots:
        raise ValueError(
            f"no valid snapshot documents found in {snapshot_dir} (yaml exists but lacks snapshot metadata)"
        )

    first = snapshots[0]
    project = first.get("project", {})
    snapshot_entries: dict[str, dict[str, Any]] = {}
    milestone_entries: dict[str, dict[str, Any]] = {}
    snapshot_order: list[str] = []
    latest_snapshot_id = None
    latest_stable_snapshot_id = None
    baseline_snapshot_id = None
    latest_roadmap_ref = None

    for path, doc in loaded:
        snap = doc["snapshot"]
        snapshot_id = snap["snapshot_id"]
        snapshot_order.append(snapshot_id)
        latest_snapshot_id = snapshot_id
        latest_roadmap_ref = snap.get("roadmap_ref") or latest_roadmap_ref
        if snap.get("kind") == "baseline" and baseline_snapshot_id is None:
            baseline_snapshot_id = snapshot_id
        if snap.get("stable"):
            latest_stable_snapshot_id = snapshot_id

        summary = _merge_index_summary(doc, snap)
        snapshot_entries[snapshot_id] = {
            "file": f"static-nervemap/snapshots/{path.name}",
            "sequence": snap.get("sequence", 0),
            "kind": snap.get("kind"),
            "generated_at": snap.get("generated_at"),
            "milestone_id": snap.get("milestone_id"),
            "stage": snap.get("stage"),
            "parent_snapshot_id": snap.get("parent_snapshot_id"),
            "commit_hash": snap.get("commit_hash"),
            "git_dirty": snap.get("git_dirty", False),
            "stable": snap.get("stable", False),
            "delta_from_parent": None,
            "summary": summary,
        }

        milestone_id = snap.get("milestone_id")
        if not milestone_id:
            continue
        milestone_title = snap.get("milestone_title") or _infer_milestone_title(
            snapshot_dir.parent.parent,
            snap.get("roadmap_ref"),
            milestone_id,
        )
        entry = milestone_entries.setdefault(
            milestone_id,
            {
                "title": milestone_title or milestone_id,
                "roadmap_ref": snap.get("roadmap_ref"),
                "status": "planned",
                "latest_snapshot_id": None,
                "latest_stable_snapshot_id": None,
                "latest_pre_snapshot_id": None,
                "latest_post_snapshot_id": None,
                "snapshot_ids": [],
                "delta_ids": [],
            },
        )
        if (not entry.get("title") or entry.get("title") == milestone_id) and milestone_title:
            entry["title"] = milestone_title
        entry["snapshot_ids"].append(snapshot_id)
        entry["latest_snapshot_id"] = snapshot_id
        stage = snap.get("stage")
        if stage == "pre":
            entry["latest_pre_snapshot_id"] = snapshot_id
        if stage == "post":
            entry["latest_post_snapshot_id"] = snapshot_id
        if snap.get("stable"):
            entry["latest_stable_snapshot_id"] = snapshot_id
            entry["status"] = "done"
        elif snapshot_id == latest_snapshot_id:
            entry["status"] = "in_progress"

    if baseline_snapshot_id is None:
        baseline_snapshot_id = snapshot_order[0]

    milestone_order = sorted(milestone_entries.keys(), key=_milestone_sort_key)
    index_doc = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "project": {
            "name": project.get("name", snapshot_dir.parent.name),
            "target_project": project.get("name", snapshot_dir.parent.name),
            "roadmap_ref": latest_roadmap_ref or "docs/ImplementationRoadmap.md#phase-7",
            "root_path": project.get("root_path", ""),
            "primary_language": project.get("primary_language", ""),
        },
        "index": {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "latest_snapshot_id": latest_snapshot_id,
            "latest_stable_snapshot_id": latest_stable_snapshot_id,
            "baseline_snapshot_id": baseline_snapshot_id,
            "snapshot_count": len(snapshot_entries),
            "milestone_count": len(milestone_entries),
        },
        "paths": {
            "snapshots_dir": "static-nervemap/snapshots",
            "deltas_dir": "static-nervemap/deltas",
        },
        "milestone_order": milestone_order,
        "snapshot_order": snapshot_order,
        "milestones": milestone_entries,
        "snapshots": snapshot_entries,
    }
    final_out = out_path or _default_index_output(snapshot_dir)
    return index_doc, final_out


def _external_file_entry(module: str):
    from .model import FileEntry
    return FileEntry(
        id=f"external:{module}",
        path=f"external:{module}",
        language="unknown",
        module=module,
        role="external_dependency",
    )


TOP_LEVEL_SUBCOMMANDS = ("analyze", "snapshot", "index")


def _add_analyze_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("repo", type=Path, help="Target repository root")
    parser.add_argument("--out", type=Path, default=None, help="Output yaml path")
    parser.add_argument("--project-name", type=str, default=None, help="Project name")
    parser.add_argument(
        "--scan-mode",
        type=str,
        choices=["full", "default", "focused"],
        default="default",
        help="Scan mode for repository traversal",
    )


def _run_analyze(args: argparse.Namespace) -> int:
    repo_root: Path = args.repo.resolve()
    if not repo_root.is_dir():
        print(f"error: not a directory: {repo_root}", file=sys.stderr)
        return 2

    project_name = args.project_name or repo_root.name
    out_path: Path = args.out or (repo_root / ".staticnervemap" / "out.yaml")

    result = analyze(repo_root, project_name, scan_mode=args.scan_mode)
    dump_yaml(result, out_path)

    print(
        f"ok: files={len(result.files)} symbols={len(result.symbols)} "
        f"relations={len(result.relations)} entrypoints={len(result.entrypoints)} "
        f"unresolved={len(result.unresolved)}"
    )
    print(f"out: {out_path}")
    return 0


def _run_snapshot_create(args: argparse.Namespace) -> int:
    repo_root: Path = args.repo.resolve()
    if not repo_root.is_dir():
        print(f"error: not a directory: {repo_root}", file=sys.stderr)
        return 2

    project_name = args.project_name or repo_root.name
    snapshot_dir = (
        args.out.parent
        if args.out is not None
        else _default_snapshot_output(repo_root, args.snapshot_id).parent
    )
    recommended_snapshot_id = suggest_snapshot_id(
        repo_root=repo_root,
        roadmap_ref=args.roadmap_ref,
        stage=args.stage or _parse_snapshot_id(args.snapshot_id)[1],
        snapshot_dir=snapshot_dir,
    )
    result, out_path = create_snapshot(
        repo_root=repo_root,
        project_name=project_name,
        snapshot_id=args.snapshot_id,
        out_path=args.out,
        parent_snapshot_id=args.parent_snapshot_id,
        milestone_id=args.milestone_id,
        stage=args.stage,
        kind=args.kind,
        roadmap_ref=args.roadmap_ref,
        change_reason=args.change_reason,
        scope_note=args.scope_note,
        scan_mode=args.scan_mode,
    )
    dump_yaml(result, out_path)
    print(
        f"ok: snapshot={args.snapshot_id} files={len(result.files)} symbols={len(result.symbols)} "
        f"relations={len(result.relations)} unresolved={len(result.unresolved)}"
    )
    if recommended_snapshot_id != args.snapshot_id:
        print(f"note: recommended_snapshot_id={recommended_snapshot_id}")
    print(f"out: {out_path}")
    return 0


def _run_snapshot_suggest_id(args: argparse.Namespace) -> int:
    repo_root: Path = args.repo.resolve()
    if not repo_root.is_dir():
        print(f"error: not a directory: {repo_root}", file=sys.stderr)
        return 2

    suggested = suggest_snapshot_id(
        repo_root=repo_root,
        roadmap_ref=args.roadmap_ref,
        stage=args.stage,
        snapshot_dir=args.snapshot_dir,
    )
    print(f"suggested_snapshot_id: {suggested}")
    return 0


def _run_index_rebuild(args: argparse.Namespace) -> int:
    snapshot_dir: Path = args.snapshot_dir.resolve()
    if not snapshot_dir.is_dir():
        print(f"error: not a directory: {snapshot_dir}", file=sys.stderr)
        return 2
    try:
        index_doc, out_path = rebuild_index(snapshot_dir, args.out)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(
            index_doc,
            f,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
            width=120,
        )
    print(
        f"ok: snapshots={index_doc['index']['snapshot_count']} milestones={index_doc['index']['milestone_count']}"
    )
    print(f"out: {out_path}")
    return 0


def _build_main_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="staticnervemap",
        description="Static analysis to yaml for AI-assisted modification bootstrap.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    analyze_parser = subparsers.add_parser(
        "analyze",
        help="Run a single-shot analysis and emit one YAML file.",
        description="Analyze a repository and emit an analysis YAML.",
    )
    _add_analyze_args(analyze_parser)
    analyze_parser.set_defaults(func=_run_analyze)

    snapshot_parser = subparsers.add_parser(
        "snapshot",
        help="Snapshot commands.",
        description="Create or inspect StaticNerveMap snapshots.",
    )
    snapshot_sub = snapshot_parser.add_subparsers(
        dest="snapshot_command", required=True, metavar="SUBCOMMAND"
    )

    create_parser = snapshot_sub.add_parser(
        "create",
        help="Create a snapshot YAML with snapshot metadata.",
        description="Create a snapshot YAML with snapshot metadata.",
    )
    create_parser.add_argument("repo", type=Path, help="Target repository root")
    create_parser.add_argument("--snapshot-id", type=str, required=True, help="Snapshot id")
    create_parser.add_argument("--out", type=Path, default=None, help="Output yaml path")
    create_parser.add_argument("--project-name", type=str, default=None, help="Project name")
    create_parser.add_argument("--parent-snapshot-id", type=str, default=None, help="Parent snapshot id")
    create_parser.add_argument("--milestone-id", type=str, default=None, help="Milestone id")
    create_parser.add_argument("--stage", type=str, default=None, help="Snapshot stage")
    create_parser.add_argument("--kind", type=str, default=None, help="Snapshot kind")
    create_parser.add_argument("--roadmap-ref", type=str, default=None, help="Roadmap reference")
    create_parser.add_argument("--change-reason", type=str, default="", help="Why this snapshot was taken")
    create_parser.add_argument("--scope-note", type=str, default="", help="Scope note")
    create_parser.add_argument(
        "--scan-mode",
        type=str,
        choices=["full", "default", "focused"],
        default="default",
        help="Scan mode for repository traversal",
    )
    create_parser.set_defaults(func=_run_snapshot_create)

    suggest_parser = snapshot_sub.add_parser(
        "suggest-id",
        help="Suggest the next snapshot id from roadmap/stage/current snapshots.",
        description="Suggest the next snapshot id from roadmap/stage/current snapshots.",
    )
    suggest_parser.add_argument("repo", type=Path, help="Target repository root")
    suggest_parser.add_argument("--roadmap-ref", type=str, default=None, help="Roadmap reference")
    suggest_parser.add_argument("--stage", type=str, default="post", help="Snapshot stage")
    suggest_parser.add_argument("--snapshot-dir", type=Path, default=None, help="Snapshot directory")
    suggest_parser.set_defaults(func=_run_snapshot_suggest_id)

    index_parser = subparsers.add_parser(
        "index",
        help="Index commands.",
        description="Rebuild or inspect the snapshot index.",
    )
    index_sub = index_parser.add_subparsers(
        dest="index_command", required=True, metavar="SUBCOMMAND"
    )
    rebuild_parser = index_sub.add_parser(
        "rebuild",
        help="Rebuild index.yaml from snapshot YAML files.",
        description="Rebuild index.yaml from snapshot YAML files.",
    )
    rebuild_parser.add_argument("snapshot_dir", type=Path, help="Snapshot directory")
    rebuild_parser.add_argument("--out", type=Path, default=None, help="Output index path")
    rebuild_parser.set_defaults(func=_run_index_rebuild)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or sys.argv[1:])

    # Backward-compat: `staticnervemap <repo>` keeps working as a default
    # single-shot analyze. When the first token is not a known subcommand
    # and not a flag, treat the whole argv as the analyze subcommand.
    if argv and argv[0] not in TOP_LEVEL_SUBCOMMANDS and not argv[0].startswith("-"):
        argv = ["analyze", *argv]

    parser = _build_main_parser()
    args = parser.parse_args(argv)
    return args.func(args)
