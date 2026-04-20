from __future__ import annotations

import hashlib
import re

from .model import FileEntry, Symbol, Unresolved


LOW_DETAIL_KEEP_PER_FILE = 3
LOW_SUMMARY_THRESHOLD = 8
MEDIUM_DETAIL_KEEP_PER_FILE = 2
MEDIUM_SUMMARY_THRESHOLD = 6
COMPRESS_TRIGGER_THRESHOLD = 500
LOW_SIGNAL_MEDIUM_METHODS = {
    "where",
    "run",
    "select",
    "orderby",
    "groupby",
    "limit",
    "offset",
    "as_dict",
    "precision",
}


def compress_unresolved(
    unresolved: list[Unresolved],
    *,
    files: list[FileEntry],
    symbols: list[Symbol],
) -> list[Unresolved]:
    if len(unresolved) < COMPRESS_TRIGGER_THRESHOLD:
        return unresolved

    symbol_to_file = {symbol.id: symbol.file_id for symbol in symbols}
    file_to_path = {file_entry.id: file_entry.path for file_entry in files}
    file_to_role = {file_entry.id: file_entry.role for file_entry in files}

    normalized = [
        _normalize_unresolved(item, symbol_to_file, file_to_role)
        for item in unresolved
    ]
    medium_or_higher = [item for item in normalized if item.severity != "low"]
    low_items = [item for item in normalized if item.severity == "low"]

    grouped: dict[str, list[Unresolved]] = {}
    for item in low_items:
        file_id = _resolve_file_id(item.target, symbol_to_file)
        grouped.setdefault(file_id, []).append(item)

    compressed_low: list[Unresolved] = []
    for file_id, items in grouped.items():
        items = sorted(items, key=lambda item: (item.line_hint or 10**9, item.id))
        if len(items) < LOW_SUMMARY_THRESHOLD:
            compressed_low.extend(items)
            continue

        compressed_low.extend(items[:LOW_DETAIL_KEEP_PER_FILE])
        summary_count = len(items) - LOW_DETAIL_KEEP_PER_FILE
        file_label = file_to_path.get(file_id, file_id.removeprefix("file:"))
        compressed_low.append(
            Unresolved(
                id=_compressed_id(file_id, summary_count),
                target=file_id,
                reason=(
                    f"{summary_count} low-signal unresolved calls compressed under "
                    f"{file_label}"
                ),
                severity="low",
                line_hint=None,
            )
        )

    compressed_medium = _compress_medium_in_test_files(
        medium_or_higher,
        symbol_to_file=symbol_to_file,
        file_to_path=file_to_path,
        file_to_role=file_to_role,
    )
    return [*compressed_medium, *compressed_low]


def _normalize_unresolved(
    item: Unresolved,
    symbol_to_file: dict[str, str],
    file_to_role: dict[str, str],
) -> Unresolved:
    file_id = _resolve_file_id(item.target, symbol_to_file)
    role = file_to_role.get(file_id)
    method_name = _extract_dynamic_method_name(item.reason)
    severity = item.severity
    if severity == "medium" and method_name in LOW_SIGNAL_MEDIUM_METHODS:
        severity = "low"
    if severity == "medium" and role == "test" and method_name in {"save", "submit", "insert", "reload", "load_from_db"}:
        severity = "low"
    if severity == item.severity:
        return item
    return Unresolved(
        id=item.id,
        target=item.target,
        reason=item.reason,
        severity=severity,
        line_hint=item.line_hint,
    )


def _compress_medium_in_test_files(
    items: list[Unresolved],
    *,
    symbol_to_file: dict[str, str],
    file_to_path: dict[str, str],
    file_to_role: dict[str, str],
) -> list[Unresolved]:
    grouped: dict[str, list[Unresolved]] = {}
    passthrough: list[Unresolved] = []
    for item in items:
        file_id = _resolve_file_id(item.target, symbol_to_file)
        if file_to_role.get(file_id) != "test":
            passthrough.append(item)
            continue
        grouped.setdefault(file_id, []).append(item)

    compressed: list[Unresolved] = []
    for file_id, group in grouped.items():
        group = sorted(group, key=lambda item: (item.line_hint or 10**9, item.id))
        if len(group) < MEDIUM_SUMMARY_THRESHOLD:
            compressed.extend(group)
            continue
        compressed.extend(group[:MEDIUM_DETAIL_KEEP_PER_FILE])
        summary_count = len(group) - MEDIUM_DETAIL_KEEP_PER_FILE
        file_label = file_to_path.get(file_id, file_id.removeprefix("file:"))
        compressed.append(
            Unresolved(
                id=_compressed_id(file_id, summary_count, prefix="compressed-medium"),
                target=file_id,
                reason=(
                    f"{summary_count} medium unresolved calls compressed under test file "
                    f"{file_label}"
                ),
                severity="medium",
                line_hint=None,
            )
        )
    return [*passthrough, *compressed]


def _resolve_file_id(target: str, symbol_to_file: dict[str, str]) -> str:
    if target.startswith("file:"):
        return target
    return symbol_to_file.get(target, target)


def _extract_dynamic_method_name(reason: str) -> str | None:
    match = re.search(r"method call: [^.]+\.(?P<method>[A-Za-z_][A-Za-z0-9_]*)$", reason)
    if match:
        return match.group("method")
    return None


def _compressed_id(file_id: str, count: int, prefix: str = "compressed") -> str:
    digest = hashlib.sha1(f"{prefix}|{file_id}|{count}".encode("utf-8")).hexdigest()[:10]
    return f"{prefix}:{digest}"
