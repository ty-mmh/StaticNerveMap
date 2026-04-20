from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import yaml

from .model import (
    AnalysisResult,
    Entrypoint,
    FileEntry,
    Note,
    Parameter,
    Relation,
    Symbol,
    Unresolved,
)


def _drop_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def _symbol_dict(s: Symbol) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": s.id,
        "kind": s.kind,
        "name": s.name,
        "qualified_name": s.qualified_name,
        "file_id": s.file_id,
        "location": {
            "start_line": s.location.start_line,
            "end_line": s.location.end_line,
        },
    }
    if s.owner_symbol_id is not None:
        d["owner_symbol_id"] = s.owner_symbol_id
    if s.decorators is not None:
        d["decorators"] = s.decorators
    if s.parameters is not None:
        d["parameters"] = [_param_dict(p) for p in s.parameters]
    if s.return_type is not None:
        d["return_type"] = s.return_type
    if s.annotation is not None:
        d["annotation"] = s.annotation
    if s.value_repr is not None:
        d["value_repr"] = s.value_repr
    if s.is_public_api:
        d["is_public_api"] = True
    if s.exported_via is not None:
        d["exported_via"] = s.exported_via
    return d


def _param_dict(p: Parameter) -> dict[str, Any]:
    return _drop_none(
        {
            "name": p.name,
            "kind": p.kind,
            "annotation": p.annotation,
            "default": p.default,
        }
    )


def _relation_dict(r: Relation) -> dict[str, Any]:
    d = {
        "id": r.id,
        "type": r.type,
        "from": r.from_id,
        "to": r.to_id,
        "confidence": r.confidence,
        "provenance": {
            "source": r.provenance.source,
            "method": r.provenance.method,
            "evidence": {
                "file_id": r.provenance.evidence.file_id,
                "line_hint": r.provenance.evidence.line_hint,
            },
        },
    }
    if r.details:
        d["details"] = r.details
    return d


def _file_dict(f: FileEntry) -> dict[str, Any]:
    return {
        "id": f.id,
        "path": f.path,
        "language": f.language,
        "module": f.module,
        "role": f.role,
    }


def _entry_dict(e: Entrypoint) -> dict[str, Any]:
    return {
        "id": e.id,
        "symbol_id": e.symbol_id,
        "kind": e.kind,
        "priority": e.priority,
        "reason": e.reason,
    }


def _unresolved_dict(u: Unresolved) -> dict[str, Any]:
    return _drop_none(
        {
            "id": u.id,
            "target": u.target,
            "reason": u.reason,
            "severity": u.severity,
            "line_hint": u.line_hint,
        }
    )


def _note_dict(n: Note) -> dict[str, Any]:
    return {
        "level": n.level,
        "target": n.target,
        "message": n.message,
    }


def build_document(result: AnalysisResult) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema_version": result.schema_version,
        "project": {
            "name": result.project.name,
            "root_path": result.project.root_path,
            "languages": result.project.languages,
            "primary_language": result.project.primary_language,
        },
        "analysis": result.analysis,
        "files": [_file_dict(f) for f in result.files],
        "symbols": [_symbol_dict(s) for s in result.symbols],
        "relations": [_relation_dict(r) for r in result.relations],
        "entrypoints": [_entry_dict(e) for e in result.entrypoints],
        "clusters": result.clusters,
        "change_targets": result.change_targets,
        "modification_paths": result.modification_paths,
        "impact_rules": result.impact_rules,
        "api_contracts": result.api_contracts,
        "unresolved": [_unresolved_dict(u) for u in result.unresolved],
        "notes": [_note_dict(n) for n in result.notes],
    }
    if result.snapshot is not None:
        doc["snapshot"] = result.snapshot
    return doc


def dump_yaml(result: AnalysisResult, out_path: Path) -> None:
    doc = build_document(result)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(
            doc,
            f,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
            width=120,
        )
