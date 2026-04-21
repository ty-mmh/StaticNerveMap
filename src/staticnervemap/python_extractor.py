from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from .model import (
    Entrypoint,
    FileEntry,
    Location,
    Note,
    Parameter,
    Relation,
    Symbol,
    Unresolved,
)
from .provenance import (
    CONF_CALL_MODULE_LEVEL,
    CONF_CALL_NESTED,
    CONF_IMPORT,
    CONF_INHERITS,
    CONF_UI_BIND,
    auto_call_resolution,
    auto_gradio_event_bind,
    auto_import,
    auto_inheritance,
    auto_ui_event_bind,
)
from .resolver import ImportBinding, Resolver

GRADIO_EVENT_METHODS = {
    "click",
    "change",
    "submit",
    "select",
    "upload",
    "release",
    "input",
    "blur",
    "stream",
    "edit",
    "play",
    "pause",
    "stop",
    "clear",
    "start_recording",
    "stop_recording",
    "then",
    "success",
}
STREAMLIT_EVENT_KWARGS = {"on_click", "on_change"}
TKINTER_COMMAND_KWARGS = {"command"}
LOW_VALUE_DYNAMIC_METHODS = {
    "add_argument",
    "append",
    "astype",
    "bind_ortvalue_input",
    "bind_output",
    "close",
    "copy",
    "cuda",
    "detach",
    "endswith",
    "eval",
    "exists",
    "extend",
    "float",
    "from_dict",
    "gather",
    "get",
    "get_inputs",
    "get_outputs",
    "group",
    "io_binding",
    "item",
    "items",
    "iterdir",
    "keys",
    "lower",
    "manual_seed",
    "mean",
    "mkdir",
    "open",
    "parameters",
    "parse_args",
    "parse_known_args",
    "pop",
    "pow",
    "read",
    "readlines",
    "relative_to",
    "replace",
    "rglob",
    "run_with_iobinding",
    "scale",
    "size",
    "sort",
    "split",
    "squeeze",
    "startswith",
    "stat",
    "state_dict",
    "step",
    "strip",
    "sub",
    "sum",
    "to",
    "train",
    "transpose",
    "unlink",
    "unscale_",
    "unsqueeze",
    "update",
    "view",
    "with_suffix",
    "write",
    "zero_",
    "zero_grad",
}

LOW_SIGNAL_DYNAMIC_METHODS = {
    "contiguous",
    "cpu",
    "find",
    "get_tensor",
    "getvalue",
    "imshow",
    "index",
    "is_dir",
    "put",
    "readline",
    "remove",
    "rename",
    "repeat",
    "reshape",
    "result",
    "search",
    "setLevel",
    "isalpha",
    "isascii",
    "upper",
    "writerow",
    "writelines",
}

PATH_LIKE_METHODS = {
    "exists",
    "is_dir",
    "is_file",
    "iterdir",
    "mkdir",
    "open",
    "relative_to",
    "resolve",
    "rglob",
    "stat",
    "unlink",
    "with_name",
    "with_stem",
    "with_suffix",
    "write_text",
    "write_bytes",
    "read_text",
    "read_bytes",
}

ROUTE_DECORATOR_ATTRS = {"route", "get", "post", "put", "patch", "delete", "options", "head"}
COMMAND_DECORATOR_ATTRS = {"command", "group"}
FRAMEWORK_ENTRY_FUNCTIONS = {
    "setup",
    "async_setup",
    "setup_entry",
    "async_setup_entry",
    "async_unload_entry",
}


def _unparse(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:
        return None


def _rel_id(*parts: str) -> str:
    h = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:10]
    return f"rel:{h}"


def _params_from_arguments(args: ast.arguments) -> list[Parameter]:
    params: list[Parameter] = []

    def _default_str(default: ast.AST | None) -> str | None:
        return _unparse(default) if default is not None else None

    posonly = args.posonlyargs
    regular = args.args
    kwonly = args.kwonlyargs

    pos_all = list(posonly) + list(regular)
    pos_defaults = list(args.defaults)
    pos_default_offset = len(pos_all) - len(pos_defaults)
    for i, a in enumerate(pos_all):
        default = None
        if i >= pos_default_offset:
            default = _default_str(pos_defaults[i - pos_default_offset])
        kind = "positional_only" if i < len(posonly) else "positional"
        params.append(
            Parameter(
                name=a.arg,
                annotation=_unparse(a.annotation),
                default=default,
                kind=kind,
            )
        )

    if args.vararg is not None:
        params.append(
            Parameter(
                name=args.vararg.arg,
                annotation=_unparse(args.vararg.annotation),
                default=None,
                kind="var_positional",
            )
        )

    for a, d in zip(kwonly, args.kw_defaults):
        params.append(
            Parameter(
                name=a.arg,
                annotation=_unparse(a.annotation),
                default=_default_str(d),
                kind="keyword_only",
            )
        )

    if args.kwarg is not None:
        params.append(
            Parameter(
                name=args.kwarg.arg,
                annotation=_unparse(args.kwarg.annotation),
                default=None,
                kind="var_keyword",
            )
        )

    return params


def _decorators(nodes: list[ast.AST]) -> list[str] | None:
    values = [_unparse(node) for node in nodes]
    values = [v for v in values if v]
    return values or None


@dataclass
class _Scope:
    kind: str  # "module", "class", "function", "method"
    name: str
    symbol_id: str | None  # None for module scope


class SymbolCollector(ast.NodeVisitor):
    """First pass: collect every class / function / method / nested function."""

    def __init__(self, file_entry: FileEntry) -> None:
        self.file = file_entry
        self.symbols: list[Symbol] = []
        self._scope_stack: list[_Scope] = [
            _Scope(kind="module", name=file_entry.module, symbol_id=None)
        ]

    def _current_qualified_prefix(self) -> str:
        parts: list[str] = []
        for s in self._scope_stack:
            if s.kind == "module":
                parts.append(s.name)
            else:
                parts.append(s.name)
        return ".".join(parts)

    def _owner_symbol_id(self) -> str | None:
        for s in reversed(self._scope_stack):
            if s.kind in ("class", "method", "function") and s.symbol_id is not None:
                return s.symbol_id
        return None

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qn = f"{self._current_qualified_prefix()}.{node.name}"
        sym = Symbol(
            id=f"class:{qn}",
            kind="class",
            name=node.name,
            qualified_name=qn,
            file_id=self.file.id,
            location=Location(start_line=node.lineno, end_line=node.end_lineno or node.lineno),
            owner_symbol_id=self._owner_symbol_id(),
            decorators=_decorators(node.decorator_list),
        )
        self.symbols.append(sym)
        self._scope_stack.append(_Scope(kind="class", name=node.name, symbol_id=sym.id))
        try:
            self.generic_visit(node)
        finally:
            self._scope_stack.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, is_async: bool) -> None:
        parent = self._scope_stack[-1]
        kind = "method" if parent.kind == "class" else "function"
        qn = f"{self._current_qualified_prefix()}.{node.name}"
        sym = Symbol(
            id=f"{kind}:{qn}",
            kind=kind,
            name=node.name,
            qualified_name=qn,
            file_id=self.file.id,
            location=Location(start_line=node.lineno, end_line=node.end_lineno or node.lineno),
            owner_symbol_id=self._owner_symbol_id(),
            decorators=_decorators(node.decorator_list),
            parameters=_params_from_arguments(node.args),
            return_type=_unparse(node.returns),
        )
        self.symbols.append(sym)
        self._scope_stack.append(_Scope(kind=kind, name=node.name, symbol_id=sym.id))
        try:
            self.generic_visit(node)
        finally:
            self._scope_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node, is_async=True)

    def visit_Assign(self, node: ast.Assign) -> None:
        if self._scope_stack[-1].kind != "module":
            self.generic_visit(node)
            return
        value_repr = _unparse(node.value)
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if not _should_emit_module_constant(target.id):
                continue
            qn = f"{self._current_qualified_prefix()}.{target.id}"
            self.symbols.append(
                Symbol(
                    id=f"constant:{qn}",
                    kind="constant",
                    name=target.id,
                    qualified_name=qn,
                    file_id=self.file.id,
                    location=Location(
                        start_line=node.lineno,
                        end_line=node.end_lineno or node.lineno,
                    ),
                    owner_symbol_id=None,
                    value_repr=value_repr,
                )
            )
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if self._scope_stack[-1].kind != "module":
            self.generic_visit(node)
            return
        if isinstance(node.target, ast.Name) and _should_emit_module_constant(node.target.id):
            qn = f"{self._current_qualified_prefix()}.{node.target.id}"
            self.symbols.append(
                Symbol(
                    id=f"constant:{qn}",
                    kind="constant",
                    name=node.target.id,
                    qualified_name=qn,
                    file_id=self.file.id,
                    location=Location(
                        start_line=node.lineno,
                        end_line=node.end_lineno or node.lineno,
                    ),
                    owner_symbol_id=None,
                    annotation=_unparse(node.annotation),
                    value_repr=_unparse(node.value),
                )
            )
        self.generic_visit(node)


@dataclass
class _RelState:
    relations: list[Relation] = field(default_factory=list)
    entrypoints: list[Entrypoint] = field(default_factory=list)
    unresolved: list[Unresolved] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    param_type_hints: dict[str, dict[str, set[str]]] = field(default_factory=dict)


class RelationCollector(ast.NodeVisitor):
    """Second pass: extract imports, calls, inherits, entrypoints."""

    def __init__(self, file_entry: FileEntry, resolver: Resolver) -> None:
        self.file = file_entry
        self.resolver = resolver
        self.state = _RelState()
        self.bindings: dict[str, ImportBinding] = {}
        self._scope_stack: list[_Scope] = [
            _Scope(kind="module", name=file_entry.module, symbol_id=None)
        ]
        self._receiver_bindings_stack: list[dict[str, str]] = [{}]
        self._package = _package_context(file_entry)

    # -------- imports --------
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            module_name = alias.name
            local_name = alias.asname or module_name.split(".")[0]
            to_id = self.resolver.resolve_module(module_name)
            is_external = to_id.startswith("external:")
            binding = ImportBinding(
                alias=local_name,
                module=module_name,
                imported_name=None,
                is_external=is_external,
                lineno=node.lineno,
            )
            self.bindings[local_name] = binding
            self._emit_import_relation(from_id=self.file.id, to_id=to_id, line=node.lineno)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module_name = self._resolve_from_module(node)
        if module_name is None:
            self.generic_visit(node)
            return
        to_id = self.resolver.resolve_module(module_name)
        is_external = to_id.startswith("external:")
        self._emit_import_relation(from_id=self.file.id, to_id=to_id, line=node.lineno)
        for alias in node.names:
            imported = alias.name
            local_name = alias.asname or imported
            if imported == "*":
                continue
            self.bindings[local_name] = ImportBinding(
                alias=local_name,
                module=module_name,
                imported_name=imported,
                is_external=is_external,
                lineno=node.lineno,
            )
        self.generic_visit(node)

    def _resolve_from_module(self, node: ast.ImportFrom) -> str | None:
        return _resolve_relative_module(node.level or 0, node.module or "", self._package)

    def _emit_import_relation(self, from_id: str, to_id: str, line: int) -> None:
        rid = _rel_id("imports", from_id, to_id, str(line))
        self.state.relations.append(
            Relation(
                id=rid,
                type="imports",
                from_id=from_id,
                to_id=to_id,
                confidence=CONF_IMPORT,
                provenance=auto_import(file_id=self.file.id, line=line),
            )
        )

    # -------- classes --------
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qn = f"{self._current_qualified_prefix()}.{node.name}"
        class_sym_id = f"class:{qn}"
        for base in node.bases:
            to = self._resolve_name_or_attr_to_symbol(base)
            if to is None:
                continue
            rid = _rel_id("inherits", class_sym_id, to, str(node.lineno))
            self.state.relations.append(
                Relation(
                    id=rid,
                    type="inherits",
                    from_id=class_sym_id,
                    to_id=to,
                    confidence=CONF_INHERITS,
                    provenance=auto_inheritance(file_id=self.file.id, line=node.lineno),
                )
            )
        self._scope_stack.append(_Scope(kind="class", name=node.name, symbol_id=class_sym_id))
        self._receiver_bindings_stack.append({})
        try:
            self.generic_visit(node)
        finally:
            self._receiver_bindings_stack.pop()
            self._scope_stack.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        parent = self._scope_stack[-1]
        kind = "method" if parent.kind == "class" else "function"
        qn = f"{self._current_qualified_prefix()}.{node.name}"
        sym_id = f"{kind}:{qn}"
        self._emit_decorator_binds(node, sym_id)
        self._maybe_emit_framework_entrypoint(node, sym_id)
        self._scope_stack.append(_Scope(kind=kind, name=node.name, symbol_id=sym_id))
        receiver_bindings = self._seed_receiver_bindings_from_params(node.args, sym_id)
        self._receiver_bindings_stack.append(receiver_bindings)
        try:
            self.generic_visit(node)
        finally:
            self._receiver_bindings_stack.pop()
            self._scope_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _current_qualified_prefix(self) -> str:
        return ".".join(s.name for s in self._scope_stack)

    # -------- calls --------
    def visit_Call(self, node: ast.Call) -> None:
        caller_id = self._current_caller_id()
        ui_bound = self._try_emit_ui_bind(node, caller_id)
        target = self._resolve_call_target(node.func)
        if target is not None:
            self._collect_param_type_hints(target, node)
            conf = (
                CONF_CALL_MODULE_LEVEL
                if self._scope_stack[-1].kind == "module"
                else CONF_CALL_NESTED
            )
            rid = _rel_id("calls", caller_id, target, str(node.lineno))
            self.state.relations.append(
                Relation(
                    id=rid,
                    type="calls",
                    from_id=caller_id,
                    to_id=target,
                    confidence=conf,
                    provenance=auto_call_resolution(file_id=self.file.id, line=node.lineno),
                )
            )
        elif not ui_bound:
            self._maybe_record_dynamic_unresolved(node, caller_id)
            self._maybe_detect_entrypoint(node)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        class_qn = self._resolve_constructor_class_qn(node.value)
        if class_qn is not None:
            for target in node.targets:
                self._bind_receiver_target(target, class_qn)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        class_qn = self._resolve_constructor_class_qn(node.value)
        if class_qn is not None:
            self._bind_receiver_target(node.target, class_qn)
        self.generic_visit(node)

    # -------- ui_binds (gradio events) --------
    def _is_gradio_file(self) -> bool:
        return any(
            b.module == "gradio" or b.module.startswith("gradio.")
            for b in self.bindings.values()
        )

    def _imports_any_prefix(self, prefixes: tuple[str, ...]) -> bool:
        return any(
            b.module == prefix or b.module.startswith(prefix + ".")
            for b in self.bindings.values()
            for prefix in prefixes
        )

    def _try_emit_ui_bind(self, node: ast.Call, caller_id: str) -> bool:
        if self._try_emit_gradio_ui_bind(node, caller_id):
            return True
        if self._try_emit_streamlit_ui_bind(node, caller_id):
            return True
        if self._try_emit_tkinter_ui_bind(node, caller_id):
            return True
        if self._try_emit_qt_ui_bind(node, caller_id):
            return True
        return False

    def _try_emit_gradio_ui_bind(self, node: ast.Call, caller_id: str) -> bool:
        func = node.func
        if not isinstance(func, ast.Attribute):
            return False
        if func.attr not in GRADIO_EVENT_METHODS:
            return False
        if not self._is_gradio_file():
            return False
        fn_value = _get_kwarg(node, "fn")
        if fn_value is None and node.args:
            fn_value = node.args[0]
        if fn_value is None:
            return False

        handler_id = self._resolve_handler(fn_value)
        event_source = _describe_event_source(func.value)
        inputs = _describe_ui_binding_values(_get_kwarg(node, "inputs"))
        outputs = _describe_ui_binding_values(_get_kwarg(node, "outputs"))
        if handler_id is not None or isinstance(fn_value, ast.Lambda):
            if handler_id is None:
                handler_id = f"inline:{self.file.id}:lambda:{node.lineno}"
            rid = _rel_id("ui_binds", caller_id, handler_id, func.attr, str(node.lineno))
            details = {
                "event_method": func.attr,
                "event_source": event_source["stable_id"],
                "event_source_expr": event_source["expr"],
                "inputs": inputs,
                "outputs": outputs,
            }
            if isinstance(fn_value, ast.Lambda):
                details["handler_kind"] = "lambda"
                details["handler_expr"] = _unparse(fn_value) or "lambda"
            self.state.relations.append(
                Relation(
                    id=rid,
                    type="ui_binds",
                    from_id=caller_id,
                    to_id=handler_id,
                    confidence=CONF_UI_BIND,
                    provenance=auto_gradio_event_bind(file_id=self.file.id, line=node.lineno),
                    details=details,
                )
            )
            return True

        reason = _describe_unresolved_handler(fn_value)
        self.state.unresolved.append(
            Unresolved(
                id=f"unresolved:{self.file.module}:ui_bind:{func.attr}:{node.lineno}",
                target=caller_id,
                reason=f"Gradio .{func.attr}() の fn 引数を静的解決できない ({reason})",
                severity="low",
            )
        )
        return True

    def _try_emit_streamlit_ui_bind(self, node: ast.Call, caller_id: str) -> bool:
        if not self._imports_any_prefix(("streamlit",)):
            return False
        kw = next((kw for kw in node.keywords if kw.arg in STREAMLIT_EVENT_KWARGS), None)
        if kw is None or kw.value is None:
            return False
        details = {
            "event_method": kw.arg,
            "event_source": _describe_event_source(node.func)["stable_id"],
            "event_source_expr": _describe_event_source(node.func)["expr"],
            "inputs": [],
            "outputs": [],
        }
        return self._emit_generic_ui_bind(
            caller_id=caller_id,
            handler_node=kw.value,
            event_method=kw.arg,
            event_source_node=node.func,
            details=details,
            line=node.lineno,
            framework="streamlit",
        )

    def _try_emit_tkinter_ui_bind(self, node: ast.Call, caller_id: str) -> bool:
        if not self._imports_any_prefix(("tkinter", "ttk", "customtkinter")):
            return False
        kw = next((kw for kw in node.keywords if kw.arg in TKINTER_COMMAND_KWARGS), None)
        if kw is None or kw.value is None:
            return False
        details = {
            "event_method": kw.arg,
            "event_source": _describe_event_source(node.func)["stable_id"],
            "event_source_expr": _describe_event_source(node.func)["expr"],
            "inputs": [],
            "outputs": [],
        }
        return self._emit_generic_ui_bind(
            caller_id=caller_id,
            handler_node=kw.value,
            event_method=kw.arg,
            event_source_node=node.func,
            details=details,
            line=node.lineno,
            framework="tkinter",
        )

    def _try_emit_qt_ui_bind(self, node: ast.Call, caller_id: str) -> bool:
        func = node.func
        if not self._imports_any_prefix(("PyQt5", "PyQt6", "PySide2", "PySide6")):
            return False
        if not isinstance(func, ast.Attribute) or func.attr != "connect":
            return False
        if not node.args:
            return False
        details = {
            "event_method": "connect",
            "event_source": _describe_event_source(func.value)["stable_id"],
            "event_source_expr": _describe_event_source(func.value)["expr"],
            "inputs": [],
            "outputs": [],
        }
        return self._emit_generic_ui_bind(
            caller_id=caller_id,
            handler_node=node.args[0],
            event_method="connect",
            event_source_node=func.value,
            details=details,
            line=node.lineno,
            framework="qt",
        )

    def _emit_generic_ui_bind(
        self,
        *,
        caller_id: str,
        handler_node: ast.AST,
        event_method: str,
        event_source_node: ast.AST,
        details: dict[str, object],
        line: int,
        framework: str,
    ) -> bool:
        handler_id = self._resolve_handler(handler_node)
        if handler_id is None and not isinstance(handler_node, ast.Lambda):
            return False
        if handler_id is None:
            handler_id = f"inline:{self.file.id}:lambda:{line}"
            details["handler_kind"] = "lambda"
            details["handler_expr"] = _unparse(handler_node) or "lambda"
        rid = _rel_id("ui_binds", caller_id, handler_id, event_method, str(line))
        self.state.relations.append(
            Relation(
                id=rid,
                type="ui_binds",
                from_id=caller_id,
                to_id=handler_id,
                confidence=CONF_UI_BIND,
                provenance=auto_ui_event_bind(file_id=self.file.id, line=line, framework=framework),
                details=details,
            )
        )
        return True

    def _resolve_handler(self, fn_node: ast.AST) -> str | None:
        if isinstance(fn_node, ast.Lambda):
            return None
        if isinstance(fn_node, ast.Name):
            return self._resolve_handler_name(fn_node.id)
        if isinstance(fn_node, ast.Attribute):
            return self._resolve_attribute(fn_node)
        return None

    def _resolve_handler_name(self, name: str) -> str | None:
        # walk current scope stack from innermost to module, looking for {prefix}.{name}
        for i in range(len(self._scope_stack), 0, -1):
            prefix = ".".join(s.name for s in self._scope_stack[:i])
            sym = self.resolver.lookup_symbol(f"{prefix}.{name}")
            if sym is not None:
                return sym.id
        # fall back to import bindings
        return self._resolve_name_binding(name)

    def _current_caller_id(self) -> str:
        for s in reversed(self._scope_stack):
            if s.symbol_id is not None:
                return s.symbol_id
        return self.file.id

    def _maybe_record_dynamic_unresolved(self, node: ast.Call, caller_id: str) -> None:
        if not isinstance(node.func, ast.Attribute):
            return
        if not isinstance(node.func.value, ast.Name):
            return
        name = node.func.value.id
        if self._should_suppress_dynamic_unresolved(name, node.func.attr):
            return
        if name in self.bindings or name in {"self", "cls"}:
            return
        if self._resolve_bound_receiver_class_qn(name) is not None:
            return
        expr = _unparse(node.func) or node.func.attr
        self.state.unresolved.append(
            Unresolved(
                id=f"unresolved:{self.file.module}:dynamic_call:{node.lineno}:{node.func.attr}",
                target=caller_id,
                reason=f"receiver 型を静的に解決できない method call: {expr}",
                severity=self._dynamic_unresolved_severity(name, node.func.attr),
                line_hint=node.lineno,
            )
        )

    def _should_suppress_dynamic_unresolved(self, receiver_name: str, method_name: str) -> bool:
        if method_name in LOW_VALUE_DYNAMIC_METHODS:
            return True
        if receiver_name == "logger" and method_name in {"debug", "info", "warning", "exception", "error"}:
            return True
        if receiver_name == "loop" and method_name == "default_exception_handler":
            return True
        if receiver_name.endswith(("_path", "_dir", "_file", "_folder")) and method_name in PATH_LIKE_METHODS:
            return True
        if receiver_name in {"p", "path"} and method_name in PATH_LIKE_METHODS:
            return True
        if receiver_name in {"app", "router"} and method_name in ROUTE_DECORATOR_ATTRS.union({"add_middleware"}):
            return True
        if receiver_name == "demo" and method_name == "queue" and self._is_gradio_file():
            return True
        if receiver_name == "executor" and method_name in {"map", "submit", "shutdown"}:
            return True
        if receiver_name == "response" and method_name in {"raise_for_status", "json", "iter_content"}:
            return True
        if receiver_name in {"q", "queue"} and method_name in {"task_done", "join", "empty", "put", "get"}:
            return True
        if receiver_name.endswith("_queue") and method_name in {"put", "get", "task_done", "empty", "join"}:
            return True
        if receiver_name in {"f", "fp", "file", "files", "writer", "reader", "sock"} and method_name in {
            "close",
            "extractall",
            "read",
            "readlines",
            "write",
            "write_text",
            "write_bytes",
            "is_file",
            "rename",
            "with_name",
        }:
            return True
        if method_name == "launch" and self._is_gradio_file():
            return True
        if receiver_name in {"parser", "args", "kwargs"} and method_name in {
            "add_argument",
            "parse_args",
            "parse_known_args",
            "get",
        }:
            return True
        return False

    def _dynamic_unresolved_severity(self, receiver_name: str, method_name: str) -> str:
        if method_name in LOW_SIGNAL_DYNAMIC_METHODS:
            return "low"
        if receiver_name.endswith("_set") and method_name == "add":
            return "low"
        if receiver_name.endswith("_queue") and method_name in {"put", "get", "task_done", "empty", "join"}:
            return "low"
        if receiver_name.endswith(("_path", "_dir", "_file", "_folder")) and method_name in {"is_dir", "rename"}:
            return "low"
        if receiver_name in {"word", "pattern", "writer", "mpl_logger", "h", "ch"}:
            return "low"
        return "medium"

    def _maybe_detect_entrypoint(self, node: ast.Call) -> None:
        if self._scope_stack[-1].kind != "module":
            return
        func = node.func
        if not isinstance(func, ast.Attribute):
            return
        if func.attr != "launch":
            return
        # heuristic: if this file imports gradio, treat module-level `.launch(...)` as gradio entry
        if not any(b.module == "gradio" or b.module.startswith("gradio.") for b in self.bindings.values()):
            return
        entry_id = f"entry:{self.file.module}-gradio"
        if any(e.id == entry_id for e in self.state.entrypoints):
            return
        self.state.entrypoints.append(
            Entrypoint(
                id=entry_id,
                symbol_id=self.file.id,
                kind="gradio_entrypoint",
                priority=1,
                reason="Gradio アプリの module-level launch() を検出",
            )
        )

    def _resolve_call_target(self, func: ast.AST) -> str | None:
        if isinstance(func, ast.Name):
            return self._resolve_name_binding(func.id)
        if isinstance(func, ast.Attribute):
            return self._resolve_attribute(func)
        return None

    def _resolve_name_binding(self, name: str) -> str | None:
        b = self.bindings.get(name)
        if b is None:
            # same-file lookup: try as top-level function or class
            candidate = f"function:{self.file.module}.{name}"
            sym = self.resolver.lookup_symbol(f"{self.file.module}.{name}")
            if sym is not None:
                return sym.id
            return None
        sym = self.resolver.resolve_from_binding(b)
        if sym is not None:
            return sym.id
        external = _external_symbol_id_from_binding(b)
        if external is not None:
            return external
        return None

    def _resolve_attribute(self, attr_node: ast.Attribute) -> str | None:
        if isinstance(attr_node.value, ast.Name) and attr_node.value.id in {"self", "cls"}:
            class_qn = self._current_class_qualified_name()
            if class_qn is not None:
                sym = self.resolver.lookup_symbol(f"{class_qn}.{attr_node.attr}")
                if sym is not None:
                    return sym.id
        if isinstance(attr_node.value, ast.Name):
            class_qn = self._resolve_bound_receiver_class_qn(attr_node.value.id)
            if class_qn is not None:
                sym = self.resolver.lookup_symbol(f"{class_qn}.{attr_node.attr}")
                if sym is not None:
                    return sym.id
        # only resolve the simple pattern: Name(module_alias).attr
        if not isinstance(attr_node.value, ast.Name):
            return None
        alias = attr_node.value.id
        binding = self.bindings.get(alias)
        if binding is None:
            return None
        if binding.imported_name is None:
            # `import mod` then `mod.func(...)`
            qn = f"{binding.module}.{attr_node.attr}"
            sym = self.resolver.lookup_symbol(qn)
            if sym is not None:
                return sym.id
            if binding.is_external:
                return f"external:{qn}"
            return None
        # `from mod import X` then `X.method(...)`
        qn = f"{binding.module}.{binding.imported_name}.{attr_node.attr}"
        sym = self.resolver.lookup_symbol(qn)
        if sym is not None:
            return sym.id
        if binding.is_external:
            return f"external:{qn}"
        return None

    def _resolve_name_or_attr_to_symbol(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Subscript):
            return self._resolve_name_or_attr_to_symbol(node.value)
        if isinstance(node, ast.Name):
            return self._resolve_name_binding(node.id)
        if isinstance(node, ast.Attribute):
            return self._resolve_attribute(node)
        return None

    def _emit_decorator_binds(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, symbol_id: str
    ) -> None:
        for deco in node.decorator_list:
            info = _decorator_bind_info(deco)
            if info is None:
                continue
            rel_type = "route_binds" if info["kind"] == "route" else "command_binds"
            rid = _rel_id(rel_type, self.file.id, symbol_id, str(node.lineno), info["decorator"])
            self.state.relations.append(
                Relation(
                    id=rid,
                    type=rel_type,
                    from_id=self.file.id,
                    to_id=symbol_id,
                    confidence=0.95,
                    provenance=auto_call_resolution(file_id=self.file.id, line=node.lineno),
                    details={
                        "decorator": info["decorator"],
                        "binder_source": info["binder_source"],
                        "bind_attr": info["bind_attr"],
                        "literal_args": info["literal_args"],
                    },
                )
            )

    def _maybe_emit_framework_entrypoint(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        symbol_id: str,
    ) -> None:
        if self._scope_stack[-1].kind != "module":
            return
        if node.name not in FRAMEWORK_ENTRY_FUNCTIONS:
            return
        entry_id = f"entry:{self.file.module}-{node.name}"
        if any(e.id == entry_id for e in self.state.entrypoints):
            return
        self.state.entrypoints.append(
            Entrypoint(
                id=entry_id,
                symbol_id=symbol_id,
                kind="framework_entrypoint",
                priority=2,
                reason=f"framework-style top-level entrypoint: {node.name}",
            )
        )

    def _current_class_qualified_name(self) -> str | None:
        class_index = None
        for i in range(len(self._scope_stack) - 1, -1, -1):
            if self._scope_stack[i].kind == "class":
                class_index = i
                break
        if class_index is None:
            return None
        return ".".join(s.name for s in self._scope_stack[: class_index + 1])

    def _bind_receiver_target(self, target: ast.AST, class_qn: str) -> None:
        if not isinstance(target, ast.Name):
            return
        self._receiver_bindings_stack[-1][target.id] = class_qn

    def _resolve_bound_receiver_class_qn(self, name: str) -> str | None:
        for bindings in reversed(self._receiver_bindings_stack):
            class_qn = bindings.get(name)
            if class_qn is not None:
                return class_qn
        return None

    def _resolve_constructor_class_qn(self, value: ast.AST | None) -> str | None:
        if not isinstance(value, ast.Call):
            return None
        target = self._resolve_call_target(value.func)
        if target is None:
            return None
        symbol = self.resolver.lookup_symbol_by_id(target)
        if symbol is None or symbol.kind != "class":
            return None
        return symbol.qualified_name

    def _seed_receiver_bindings_from_params(self, args: ast.arguments, symbol_id: str) -> dict[str, str]:
        receiver_bindings: dict[str, str] = {}
        all_args = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
        if args.vararg is not None:
            all_args.append(args.vararg)
        if args.kwarg is not None:
            all_args.append(args.kwarg)
        for arg in all_args:
            if arg.arg in {"self", "cls"}:
                continue
            class_qn = self._resolve_annotation_class_qn(arg.annotation)
            if class_qn is None:
                class_qn = self.resolver.get_inferred_param_type(symbol_id, arg.arg)
            if class_qn is not None:
                receiver_bindings[arg.arg] = class_qn
        return receiver_bindings

    def _resolve_annotation_class_qn(self, annotation: ast.AST | None) -> str | None:
        if annotation is None:
            return None
        target = self._resolve_name_or_attr_to_symbol(annotation)
        if target is None:
            return None
        symbol = self.resolver.lookup_symbol_by_id(target)
        if symbol is None or symbol.kind != "class":
            return None
        return symbol.qualified_name

    def _collect_param_type_hints(self, target_id: str, node: ast.Call) -> None:
        symbol = self.resolver.lookup_symbol_by_id(target_id)
        if symbol is None or not symbol.parameters:
            return
        params = list(symbol.parameters)
        if symbol.kind == "method" and params and params[0].name in {"self", "cls"}:
            params = params[1:]

        for index, arg in enumerate(node.args):
            if index >= len(params):
                break
            class_qn = self._resolve_argument_class_qn(arg)
            if class_qn is None:
                continue
            self._record_param_type_hint(symbol.id, params[index].name, class_qn)

        params_by_name = {param.name: param for param in params}
        for kw in node.keywords:
            if kw.arg is None:
                continue
            param = params_by_name.get(kw.arg)
            if param is None:
                continue
            class_qn = self._resolve_argument_class_qn(kw.value)
            if class_qn is None:
                continue
            self._record_param_type_hint(symbol.id, param.name, class_qn)

    def _resolve_argument_class_qn(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return self._resolve_bound_receiver_class_qn(node.id)
        if isinstance(node, ast.Call):
            return self._resolve_constructor_class_qn(node)
        return None

    def _record_param_type_hint(self, symbol_id: str, param_name: str, class_qn: str) -> None:
        by_symbol = self.state.param_type_hints.setdefault(symbol_id, {})
        by_param = by_symbol.setdefault(param_name, set())
        by_param.add(class_qn)

    # -------- if __name__ == "__main__" --------
    def visit_If(self, node: ast.If) -> None:
        if self._scope_stack[-1].kind == "module" and _is_name_main_test(node.test):
            entry_id = f"entry:{self.file.module}-main"
            if not any(e.id == entry_id for e in self.state.entrypoints):
                self.state.entrypoints.append(
                    Entrypoint(
                        id=entry_id,
                        symbol_id=self.file.id,
                        kind="script_main",
                        priority=1,
                        reason='if __name__ == "__main__": を検出',
                    )
                )
        self.generic_visit(node)


def _is_name_main_test(node: ast.AST) -> bool:
    if not isinstance(node, ast.Compare):
        return False
    if len(node.ops) != 1 or not isinstance(node.ops[0], ast.Eq):
        return False
    left = node.left
    right = node.comparators[0]

    def _is_name(n: ast.AST) -> bool:
        return isinstance(n, ast.Name) and n.id == "__name__"

    def _is_main(n: ast.AST) -> bool:
        return isinstance(n, ast.Constant) and n.value == "__main__"

    return (_is_name(left) and _is_main(right)) or (_is_name(right) and _is_main(left))


def _package_of(module: str) -> str:
    if "." not in module:
        return ""
    return module.rsplit(".", 1)[0]


def _package_context(file_entry: FileEntry) -> str:
    if file_entry.path.endswith("__init__.py"):
        return file_entry.module
    return _package_of(file_entry.module)


def _resolve_relative_module(level: int, base: str, pkg: str) -> str | None:
    """Resolve a relative `from ... import` target into an absolute module name.

    `level` is `node.level` (1+ for relative imports), `base` is `node.module or ""`,
    and `pkg` is the importing file's current package as returned by
    `_package_context`. Returns the absolute module path, or None when the import
    climbs above the repository root with no remaining base name.
    """
    if level <= 0:
        return base or None
    pkg_parts = pkg.split(".") if pkg else []
    keep = len(pkg_parts) - (level - 1)
    if keep < 0:
        return base or None
    anchor = ".".join(pkg_parts[:keep]) if keep > 0 else ""
    if base:
        return f"{anchor}.{base}" if anchor else base
    return anchor or None


class ExportCollector(ast.NodeVisitor):
    def __init__(self, file_entry: FileEntry, resolver: Resolver) -> None:
        self.file = file_entry
        self.resolver = resolver
        self.bindings: dict[str, ImportBinding] = {}
        self.exported_symbol_ids: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            module_name = alias.name
            local_name = alias.asname or module_name.split(".")[0]
            self.bindings[local_name] = ImportBinding(
                alias=local_name,
                module=module_name,
                imported_name=None,
                is_external=not self.resolver.is_local_module(module_name),
                lineno=node.lineno,
            )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module_name = self._resolve_from_module(node)
        if module_name is None:
            self.generic_visit(node)
            return
        is_external = not self.resolver.is_local_module(module_name)
        for alias in node.names:
            imported = alias.name
            if imported == "*":
                continue
            local_name = alias.asname or imported
            self.bindings[local_name] = ImportBinding(
                alias=local_name,
                module=module_name,
                imported_name=imported,
                is_external=is_external,
                lineno=node.lineno,
            )
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "__all__":
                self._record_exports(node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and node.target.id == "__all__":
            self._record_exports(node.value)
        self.generic_visit(node)

    def _resolve_from_module(self, node: ast.ImportFrom) -> str | None:
        return _resolve_relative_module(
            node.level or 0, node.module or "", _package_context(self.file)
        )

    def _record_exports(self, value: ast.AST | None) -> None:
        if value is None:
            return
        for name in _string_list_literal(value):
            symbol = self._resolve_export_name(name)
            if symbol is not None:
                self.exported_symbol_ids.add(symbol.id)

    def _resolve_export_name(self, name: str):
        binding = self.bindings.get(name)
        if binding is not None:
            resolved = self.resolver.resolve_from_binding(binding)
            if resolved is not None:
                return resolved
        return self.resolver.lookup_symbol(f"{self.file.module}.{name}")


def _get_kwarg(call: ast.Call, name: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _string_list_literal(node: ast.AST) -> list[str]:
    if not isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return []
    values: list[str] = []
    for elt in node.elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            values.append(elt.value)
    return values


def _describe_unresolved_handler(node: ast.AST) -> str:
    if isinstance(node, ast.Lambda):
        return "lambda"
    if isinstance(node, ast.Call):
        return "call expression"
    if isinstance(node, ast.Attribute):
        return f"attribute chain {ast.unparse(node)}"
    if isinstance(node, ast.Name):
        return f"unresolved name {node.id}"
    return type(node).__name__


def _decorator_bind_info(node: ast.AST) -> dict[str, object] | None:
    if isinstance(node, ast.Call):
        func = node.func
        literal_args = [_literal_arg(a) for a in node.args]
    else:
        func = node
        literal_args = []

    if not isinstance(func, ast.Attribute):
        return None
    if not isinstance(func.value, ast.Name):
        return None

    binder_source = func.value.id
    bind_attr = func.attr
    decorator_text = _unparse(node) or _unparse(func) or bind_attr

    if bind_attr in ROUTE_DECORATOR_ATTRS:
        return {
            "kind": "route",
            "decorator": decorator_text,
            "binder_source": binder_source,
            "bind_attr": bind_attr,
            "literal_args": [a for a in literal_args if a is not None],
        }
    if bind_attr in COMMAND_DECORATOR_ATTRS:
        return {
            "kind": "command",
            "decorator": decorator_text,
            "binder_source": binder_source,
            "bind_attr": bind_attr,
            "literal_args": [a for a in literal_args if a is not None],
        }
    return None


def _literal_arg(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant):
        return repr(node.value)
    return None


def _describe_event_source(node: ast.AST) -> dict[str, str]:
    try:
        text = ast.unparse(node)
    except Exception:
        text = type(node).__name__
    if isinstance(node, ast.Name):
        stable_id = node.id
    elif isinstance(node, ast.Attribute):
        stable_id = text
    else:
        stable_id = text
    return {"stable_id": stable_id, "expr": text}


def _describe_ui_binding_values(node: ast.AST | None) -> list[str]:
    if node is None:
        return []
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_describe_ui_binding_item(elt) for elt in node.elts]
    return [_describe_ui_binding_item(node)]


def _describe_ui_binding_item(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        try:
            return ast.unparse(node)
        except Exception:
            return node.attr
    try:
        return ast.unparse(node)
    except Exception:
        return type(node).__name__


def _external_symbol_id_from_binding(binding: ImportBinding) -> str | None:
    if not binding.is_external:
        return None
    if binding.imported_name is not None:
        return f"external:{binding.module}.{binding.imported_name}"
    return f"external:{binding.module}"


def _should_emit_module_constant(name: str) -> bool:
    if name.isupper():
        return True
    if name in {"__all__", "__version__"}:
        return True
    return False


def parse_file(path: Path) -> ast.Module | None:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return ast.parse(source, filename=str(path))
    except SyntaxError:
        return None


def collect_symbols_for_file(tree: ast.Module, file_entry: FileEntry) -> list[Symbol]:
    collector = SymbolCollector(file_entry)
    collector.visit(tree)
    return collector.symbols


def collect_relations_for_file(
    tree: ast.Module, file_entry: FileEntry, resolver: Resolver
) -> _RelState:
    collector = RelationCollector(file_entry, resolver)
    collector.visit(tree)
    return collector.state


def finalize_param_type_hints(
    states: list[_RelState],
) -> dict[str, dict[str, str]]:
    merged: dict[str, dict[str, set[str]]] = {}
    for state in states:
        for symbol_id, params in state.param_type_hints.items():
            by_symbol = merged.setdefault(symbol_id, {})
            for param_name, class_qns in params.items():
                by_param = by_symbol.setdefault(param_name, set())
                by_param.update(class_qns)

    finalized: dict[str, dict[str, str]] = {}
    for symbol_id, params in merged.items():
        for param_name, class_qns in params.items():
            if len(class_qns) != 1:
                continue
            finalized.setdefault(symbol_id, {})[param_name] = next(iter(class_qns))
    return finalized


def collect_public_exports_for_file(
    tree: ast.Module, file_entry: FileEntry, resolver: Resolver
) -> set[str]:
    collector = ExportCollector(file_entry, resolver)
    collector.visit(tree)
    return collector.exported_symbol_ids
