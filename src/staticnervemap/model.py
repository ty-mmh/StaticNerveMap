from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Project:
    name: str
    root_path: str
    languages: list[str]
    primary_language: str


@dataclass
class FileEntry:
    id: str
    path: str
    language: str
    module: str
    role: str


@dataclass
class Location:
    start_line: int
    end_line: int


@dataclass
class Parameter:
    name: str
    annotation: str | None = None
    default: str | None = None
    kind: str = "positional"


@dataclass
class Symbol:
    id: str
    kind: str
    name: str
    qualified_name: str
    file_id: str
    location: Location
    owner_symbol_id: str | None = None
    decorators: list[str] | None = None
    parameters: list[Parameter] | None = None
    return_type: str | None = None
    annotation: str | None = None
    value_repr: str | None = None
    is_public_api: bool = False
    exported_via: list[str] | None = None


@dataclass
class Evidence:
    file_id: str
    line_hint: int


@dataclass
class Provenance:
    source: str
    method: str
    evidence: Evidence


@dataclass
class Relation:
    id: str
    type: str
    from_id: str
    to_id: str
    confidence: float
    provenance: Provenance
    details: dict[str, Any] | None = None


@dataclass
class Entrypoint:
    id: str
    symbol_id: str
    kind: str
    priority: int
    reason: str


@dataclass
class Unresolved:
    id: str
    target: str
    reason: str
    severity: str
    line_hint: int | None = None


@dataclass
class Note:
    level: str
    target: str
    message: str


@dataclass
class AnalysisResult:
    project: Project
    analysis: dict[str, Any]
    schema_version: str = "0.2"
    snapshot: dict[str, Any] | None = None
    files: list[FileEntry] = field(default_factory=list)
    symbols: list[Symbol] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    entrypoints: list[Entrypoint] = field(default_factory=list)
    clusters: list[dict[str, Any]] = field(default_factory=list)
    change_targets: list[dict[str, Any]] = field(default_factory=list)
    modification_paths: list[dict[str, Any]] = field(default_factory=list)
    impact_rules: list[dict[str, Any]] = field(default_factory=list)
    api_contracts: list[dict[str, Any]] = field(default_factory=list)
    unresolved: list[Unresolved] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
