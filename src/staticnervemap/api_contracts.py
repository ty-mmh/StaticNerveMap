from __future__ import annotations

from .model import FileEntry, Relation, Symbol


def build_api_contracts(
    files: list[FileEntry], symbols: list[Symbol], relations: list[Relation]
) -> list[dict[str, object]]:
    contracts: list[dict[str, object]] = []
    file_map = {f.id: f for f in files}
    symbol_counts_by_file: dict[str, int] = {}
    non_import_relation_counts: dict[str, int] = {}
    external_imports_by_file = _external_imports_by_file(relations)
    symbol_to_file_id = {symbol.id: symbol.file_id for symbol in symbols}
    boundary_surface_files = _boundary_surface_files(relations, symbol_to_file_id)

    for symbol in symbols:
        if symbol.kind in {"function", "method", "class"}:
            symbol_counts_by_file[symbol.file_id] = symbol_counts_by_file.get(symbol.file_id, 0) + 1

    for rel in relations:
        if rel.type == "imports":
            continue
        for side in (rel.from_id, rel.to_id):
            if side.startswith("file:"):
                non_import_relation_counts[side] = non_import_relation_counts.get(side, 0) + 1

    for symbol in symbols:
        if symbol.kind not in {"function", "method"}:
            continue

        qn = symbol.qualified_name

        if symbol.return_type:
            contracts.append(
                {
                    "id": f"contract:return:{qn}",
                    "symbol_id": symbol.id,
                    "kind": "return_annotation",
                    "summary": f"戻り値注釈は `{symbol.return_type}`",
                    "confidence": 0.95,
                }
            )

        if symbol.parameters:
            typed_params = [p for p in symbol.parameters if p.annotation]
            if typed_params:
                param_desc = ", ".join(f"{p.name}: {p.annotation}" for p in typed_params[:4])
                contracts.append(
                    {
                        "id": f"contract:params:{qn}",
                        "symbol_id": symbol.id,
                        "kind": "parameter_contract",
                        "summary": f"主要パラメータ注釈: {param_desc}",
                        "confidence": 0.9,
                    }
                )

        if _looks_like_generator(symbol):
            contracts.append(
                {
                    "id": f"contract:generator:{qn}",
                    "symbol_id": symbol.id,
                    "kind": "generator_protocol",
                    "summary": "generator 的に途中経過や完了結果を順次返す可能性が高い",
                    "confidence": 0.75,
                }
            )

    for file_id in sorted({symbol.file_id for symbol in symbols}):
        file_entry = file_map.get(file_id)
        if not file_entry:
            continue
        externals = sorted(external_imports_by_file.get(file_id, set()))
        if not externals:
            continue
        if _is_external_api_hotspot(
            path=file_entry.path,
            externals=externals,
            callable_symbol_count=symbol_counts_by_file.get(file_id, 0),
            interaction_count=non_import_relation_counts.get(file_id, 0),
            is_boundary_surface=file_id in boundary_surface_files,
        ):
            contracts.append(
                {
                    "id": f"contract:external:{file_entry.module}",
                    "symbol_id": file_id,
                    "kind": "external_api_assumption",
                    "summary": f"外部依存 `{', '.join(externals[:4])}` の API 契約に影響される",
                    "confidence": 0.8,
                }
            )

    return _dedupe_contracts(contracts)


def _looks_like_generator(symbol: Symbol) -> bool:
    return (
        symbol.kind == "function"
        and (
            "batch_" in symbol.name
            or symbol.name.startswith("stream_")
            or symbol.name.startswith("iter_")
        )
    )


def _is_external_api_hotspot(
    path: str,
    externals: list[str],
    *,
    callable_symbol_count: int,
    interaction_count: int,
    is_boundary_surface: bool,
) -> bool:
    if is_boundary_surface:
        return False
    if _looks_like_support_path(path):
        return False
    if len(externals) >= 3 and callable_symbol_count >= 2:
        return True
    if callable_symbol_count >= 4 and len(externals) >= 2:
        return True
    if interaction_count >= 6 and len(externals) >= 2:
        return True
    return False


def _external_imports_by_file(relations: list[Relation]) -> dict[str, set[str]]:
    by_file: dict[str, set[str]] = {}
    for rel in relations:
        if rel.type != "imports" or not rel.to_id.startswith("external:"):
            continue
        if not rel.from_id.startswith("file:"):
            continue
        by_file.setdefault(rel.from_id, set()).add(rel.to_id.removeprefix("external:"))
    return by_file


def _boundary_surface_files(
    relations: list[Relation],
    symbol_to_file_id: dict[str, str],
) -> set[str]:
    file_ids: set[str] = set()
    for rel in relations:
        if rel.type not in {"ui_binds", "route_binds", "command_binds"}:
            continue
        file_id = symbol_to_file_id.get(rel.from_id, rel.from_id if rel.from_id.startswith("file:") else "")
        if file_id.startswith("file:"):
            file_ids.add(file_id)
    return file_ids


def _looks_like_support_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return name in {"__init__.py", "utils.py", "helpers.py", "common.py", "config.py"}


def _dedupe_contracts(contracts: list[dict[str, object]]) -> list[dict[str, object]]:
    seen: set[str] = set()
    deduped: list[dict[str, object]] = []
    for contract in contracts:
        cid = str(contract["id"])
        if cid in seen:
            continue
        seen.add(cid)
        deduped.append(contract)
    return deduped
