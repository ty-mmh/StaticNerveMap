from __future__ import annotations

from .model import Evidence, Provenance


def auto_import(file_id: str, line: int) -> Provenance:
    return Provenance(
        source="auto",
        method="ast_import_resolution",
        evidence=Evidence(file_id=file_id, line_hint=line),
    )


def auto_call_resolution(file_id: str, line: int) -> Provenance:
    return Provenance(
        source="auto",
        method="ast_call_resolution",
        evidence=Evidence(file_id=file_id, line_hint=line),
    )


def auto_inheritance(file_id: str, line: int) -> Provenance:
    return Provenance(
        source="auto",
        method="ast_class_bases",
        evidence=Evidence(file_id=file_id, line_hint=line),
    )


def auto_gradio_event_bind(file_id: str, line: int) -> Provenance:
    return Provenance(
        source="auto",
        method="ast_gradio_event_bind",
        evidence=Evidence(file_id=file_id, line_hint=line),
    )


def auto_ui_event_bind(file_id: str, line: int, framework: str) -> Provenance:
    return Provenance(
        source="auto",
        method=f"ast_{framework}_event_bind",
        evidence=Evidence(file_id=file_id, line_hint=line),
    )


# confidence constants
CONF_IMPORT = 1.0
CONF_CALL_MODULE_LEVEL = 0.95
CONF_CALL_NESTED = 0.9
CONF_INHERITS = 1.0
CONF_UI_BIND = 0.95
