from __future__ import annotations

from dataclasses import dataclass

from .model import FileEntry, Symbol


@dataclass
class ImportBinding:
    """One local alias name introduced by an import statement."""
    alias: str
    module: str
    imported_name: str | None  # None for `import x` or `import x as y`; name for `from x import y`
    is_external: bool
    lineno: int


class Resolver:
    """Global lookup tables + per-file import bindings."""

    def __init__(self, files: list[FileEntry], symbols: list[Symbol]) -> None:
        self.files = files
        self.module_to_file: dict[str, FileEntry] = {f.module: f for f in files}
        self.qualified_to_symbol: dict[str, Symbol] = {s.qualified_name: s for s in symbols}
        self.id_to_symbol: dict[str, Symbol] = {s.id: s for s in symbols}
        self.external_modules: set[str] = set()
        self.inferred_param_types: dict[str, dict[str, str]] = {}

    def is_local_module(self, module_dotted: str) -> bool:
        if module_dotted in self.module_to_file:
            return True
        # partial match for package modules
        for mod in self.module_to_file:
            if module_dotted == mod or mod.startswith(module_dotted + "."):
                return True
        return False

    def resolve_module(self, module_dotted: str) -> str:
        """Return a file_id or external:<module> marker."""
        if module_dotted in self.module_to_file:
            return self.module_to_file[module_dotted].id
        # walk up parents for package imports
        parts = module_dotted.split(".")
        while parts:
            candidate = ".".join(parts)
            if candidate in self.module_to_file:
                return self.module_to_file[candidate].id
            parts.pop()
        self.external_modules.add(module_dotted)
        return f"external:{module_dotted}"

    def lookup_symbol(self, qualified_name: str) -> Symbol | None:
        return self.qualified_to_symbol.get(qualified_name)

    def lookup_symbol_by_id(self, symbol_id: str) -> Symbol | None:
        return self.id_to_symbol.get(symbol_id)

    def get_inferred_param_type(self, symbol_id: str, param_name: str) -> str | None:
        return self.inferred_param_types.get(symbol_id, {}).get(param_name)

    def set_inferred_param_types(self, param_types: dict[str, dict[str, str]]) -> None:
        self.inferred_param_types = param_types

    def resolve_from_binding(self, binding: ImportBinding) -> Symbol | None:
        """Given an import binding, return the target symbol if it is a local class/function."""
        if binding.is_external:
            return None
        if binding.imported_name is None:
            return None
        qn = f"{binding.module}.{binding.imported_name}"
        return self.qualified_to_symbol.get(qn)
