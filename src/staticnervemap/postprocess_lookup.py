from __future__ import annotations

from dataclasses import dataclass

from .model import FileEntry, Relation, Symbol


@dataclass(frozen=True)
class FileLookup:
    file_map: dict[str, FileEntry]
    local_files: list[FileEntry]
    local_file_ids: set[str]
    module_to_file_id: dict[str, str]

    def resolve_file_id(self, symbol_or_file_id: str) -> str | None:
        if symbol_or_file_id in self.file_map:
            return symbol_or_file_id
        if ":" not in symbol_or_file_id:
            return None
        _, qualified_name = symbol_or_file_id.split(":", 1)
        parts = qualified_name.split(".")
        while parts:
            candidate = ".".join(parts)
            file_id = self.module_to_file_id.get(candidate)
            if file_id is not None:
                return file_id
            parts.pop()
        return None


def build_file_lookup(files: list[FileEntry]) -> FileLookup:
    file_map = {f.id: f for f in files}
    local_files = [f for f in files if f.id.startswith("file:")]
    module_to_file_id = {
        file_entry.module: file_entry.id
        for file_entry in local_files
        if file_entry.module
    }
    return FileLookup(
        file_map=file_map,
        local_files=local_files,
        local_file_ids={file_entry.id for file_entry in local_files},
        module_to_file_id=module_to_file_id,
    )


def resolve_relation_file_ids(
    relations: list[Relation],
    lookup: FileLookup,
) -> list[tuple[Relation, str | None, str | None]]:
    return [
        (rel, lookup.resolve_file_id(rel.from_id), lookup.resolve_file_id(rel.to_id))
        for rel in relations
    ]


def build_symbol_file_map(symbols: list[Symbol]) -> dict[str, str]:
    return {
        symbol.id: symbol.file_id
        for symbol in symbols
        if symbol.file_id
    }
