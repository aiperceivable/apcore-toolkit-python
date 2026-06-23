"""Byte-equivalent tabular data formatters: CSV and JSONL.

Cross-SDK byte-identity contract: every SDK (Python / TypeScript / Rust)
emits identical bytes for the same input. Consumers (apcore-cli, apcore-mcp,
apcore-a2a, downstream CLIs) MUST delegate to these formatters rather than
reimplementing.

See ``apcore-toolkit/docs/features/formatting.md`` § Tabular Formats.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from typing import Any

_BOM = "\ufeff"


def format_csv(
    rows: Iterable[Mapping[str, Any]],
    *,
    bom: bool = False,
) -> str:
    """Render rows as RFC 4180 CSV.

    Header columns are the **union of keys across all rows**, preserved in
    insertion order from first occurrence. Rows missing a key emit an empty
    cell. Non-scalar values are serialized as canonical JSON inside the cell.
    Cells containing ``,``, ``"``, ``\\n``, or ``\\r`` are quote-wrapped with
    embedded ``"`` doubled. Line terminator is CRLF.

    Args:
        rows: Iterable of dict-like records. Empty iterable returns ``""``.
        bom: When ``True``, prepend a UTF-8 BOM for Excel-locale users.
            Default ``False``.

    Returns:
        CSV text terminated by CRLF; empty string for empty input.
    """
    rows_list = list(rows)
    if not rows_list:
        return ""

    keys: list[str] = []
    seen: set[str] = set()
    for row in rows_list:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                keys.append(key)

    lines: list[str] = [_csv_join(keys)]
    for row in rows_list:
        cells = [_csv_cell(row.get(key)) for key in keys]
        lines.append(_csv_join(cells))

    body = "\r\n".join(lines) + "\r\n"
    return (_BOM + body) if bom else body


def format_jsonl(rows: Iterable[Mapping[str, Any]]) -> str:
    """Render rows as JSON Lines.

    Each row is serialized via canonical compact JSON (no spaces between
    separators, ``ensure_ascii=False``, insertion-order preserved). Lines are
    terminated by LF (not CRLF — JSONL convention). No trailing blank line.

    Args:
        rows: Iterable of dict-like records. Empty iterable returns ``""``.

    Returns:
        JSONL text; empty string for empty input.
    """
    rows_list = list(rows)
    if not rows_list:
        return ""
    return "\n".join(_canonical_json(row) for row in rows_list) + "\n"


def _csv_join(cells: list[str]) -> str:
    return ",".join(_csv_escape(c) for c in cells)


def _csv_escape(value: str) -> str:
    if any(ch in value for ch in (",", '"', "\n", "\r")):
        return '"' + value.replace('"', '""') + '"'
    return value


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return ""
        return _canonical_number(value)
    if isinstance(value, (dict, list, tuple)):
        return _canonical_json(value)
    return str(value)


def _canonical_json(value: Any) -> str:
    """Canonical compact JSON aligned with JS ``JSON.stringify``:

    - Whole-number floats (``1.0``) emit as ``1`` (no trailing ``.0``)
    - ``NaN`` / ``±Infinity`` emit as ``null`` (matches JS behavior; raw
      ``json.dumps`` would either raise or emit invalid JSON tokens)
    - Object keys preserved in insertion order; no key sorting
    - Unicode preserved unescaped (``ensure_ascii=False``)
    """
    return json.dumps(
        _canonicalize_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=False,
    )


def _canonicalize_value(value: Any) -> Any:
    """Recursively coerce a JSON-shaped value into the form that matches JS
    ``JSON.stringify`` output, so canonical JSON emission is byte-identical
    across SDKs.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        if value == int(value):
            return int(value)
        return value
    if isinstance(value, (list, tuple)):
        return [_canonicalize_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _canonicalize_value(v) for k, v in value.items()}
    return value


def _canonical_number(value: float) -> str:
    """Render a finite float matching JS/Rust default number stringification:
    whole-number floats render as plain integers (``1.0`` → ``"1"``); fractional
    values use Python's shortest round-trip repr. This drops the Python int/float
    distinction in CSV output, which is acceptable: CSV has no type system.
    """
    if value == int(value) and not math.isnan(value):
        return str(int(value))
    return repr(value)
