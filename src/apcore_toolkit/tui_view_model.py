"""TuiViewModel — Tier-1 byte-equivalent module-list view shape.

Lifts the *shape* of a module-list view (columns, rows, filter intent, sort
intent, color-by-tag rules) into the toolkit, so every downstream consumer
(``apcore-cli-*``, future browser dashboards, MCP/A2A surfaces) produces
identical column sets, identical filter semantics, and identical row order
for the same ``ScannedModule`` input. Rendering itself stays Tier 2 and is
free to differ in pixels.

See ``apcore-toolkit/docs/features/tui-view-model.md`` for the full V1
specification, wire format, and conformance corpus.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from apcore_toolkit.types import ScannedModule

__all__ = [
    "Cell",
    "Column",
    "Filter",
    "Group",
    "Row",
    "Sort",
    "ToneRule",
    "TonePalette",
    "TuiViewModel",
    "format_view_model",
    "modules_to_view_model",
]

View = Literal["list", "grouped"]
Justify = Literal["left", "right", "center"]
Exposure = Literal["exposed", "hidden", "all"]
Direction = Literal["asc", "desc"]
Tone = Literal["neutral", "positive", "negative", "warning", "info"]
CellKind = Literal["text", "tags", "badge", "symbol"]
GroupBy = Literal["tag", "prefix"]

_SORTABLE_KEYS: frozenset[str] = frozenset({"module_id", "alias", "description"})

# The 9 canonical boolean flags on ModuleAnnotations (apcore package) that
# `Filter.annotations` is allowed to reference. Any other attribute name
# (e.g. `cache_ttl`, `cache_key_fields`, `pagination_style`, `extra` — all
# non-boolean fields on ModuleAnnotations) is not a recognized filter flag
# and must not be reflected via getattr(); matches the Rust SDK's hardcoded
# `match name { "readonly" => ..., _ => false }` in tui_view_model.rs.
_FILTERABLE_ANNOTATION_FLAGS: frozenset[str] = frozenset(
    {
        "readonly",
        "destructive",
        "idempotent",
        "requires_approval",
        "open_world",
        "streaming",
        "cacheable",
        "paginated",
        "discoverable",
    }
)

# No built-in default column set: `columns` must be explicitly requested by
# the caller. An empty `columns` tuple yields an empty `columns` array (see
# conformance fixture `view_model_001_empty_list`) — callers wanting the
# conventional ID/description/tags layout pass it explicitly, e.g.
# `columns=("module_id", "description", "tags")`.
_COLUMN_LABELS: dict[str, str] = {
    "module_id": "ID",
    "alias": "Alias",
    "description": "Description",
    "tags": "Tags",
}


@dataclass(frozen=True)
class Cell:
    """A single table cell (discriminated union by ``kind``)."""

    kind: CellKind
    value: str | None = None
    values: list[str] | None = None
    tone: Tone | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": self.kind}
        if self.kind == "tags":
            d["values"] = list(self.values or [])
        else:
            d["value"] = self.value or ""
        if self.tone is not None:
            d["tone"] = self.tone
        return d


@dataclass(frozen=True)
class Column:
    """A view-model column: render order and ``Row.cells`` index lookup."""

    key: str
    label: str
    justify: Justify = "left"
    tone_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"key": self.key, "label": self.label}
        if self.justify != "left":
            d["justify"] = self.justify
        if self.tone_by is not None:
            d["tone_by"] = self.tone_by
        return d


@dataclass(frozen=True)
class Row:
    """A view-model row. ``cells[i]`` corresponds to ``columns[i]``."""

    cells: list[Cell]
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"cells": [c.to_dict() for c in self.cells]}
        if self.tags:
            d["tags"] = list(self.tags)
        return d


@dataclass(frozen=True)
class Sort:
    """Annotates which sort the toolkit (or the caller) applied."""

    key: str
    direction: Direction = "asc"

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "direction": self.direction}


@dataclass(frozen=True)
class Filter:
    """Annotates which filter the toolkit applied. All fields required."""

    tags: tuple[str, ...] = ()
    search: str = ""
    annotations: tuple[str, ...] = ()
    exposure: Exposure = "all"
    deprecated: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "tags": list(self.tags),
            "search": self.search,
            "annotations": list(self.annotations),
            "exposure": self.exposure,
            "deprecated": self.deprecated,
        }


@dataclass(frozen=True)
class ToneRule:
    """First-match-wins rule mapping a tag to a semantic tone."""

    value: str
    tone: Tone
    kind: Literal["tag_equals"] = "tag_equals"

    def to_dict(self) -> dict[str, Any]:
        return {"match": {"kind": self.kind, "value": self.value}, "tone": self.tone}


@dataclass(frozen=True)
class TonePalette:
    """A named, ordered set of :class:`ToneRule`."""

    name: str
    rules: list[ToneRule] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "rules": [r.to_dict() for r in self.rules]}


@dataclass(frozen=True)
class Group:
    """A named group of row indices, present only when ``kind == 'grouped'``."""

    label: str
    row_indices: list[int]

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "row_indices": list(self.row_indices)}


@dataclass(frozen=True)
class TuiViewModel:
    """The V1 ``TuiViewModel`` wire-format envelope."""

    kind: View
    columns: list[Column]
    rows: list[Row]
    schema_version: int = 1
    title: str | None = None
    groups: list[Group] | None = None
    sort: Sort | None = None
    filter: Filter | None = None
    tone_palettes: list[TonePalette] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "schema_version": self.schema_version,
            "kind": self.kind,
        }
        if self.title is not None:
            d["title"] = self.title
        d["columns"] = [c.to_dict() for c in self.columns]
        d["rows"] = [r.to_dict() for r in self.rows]
        if self.groups is not None:
            d["groups"] = [g.to_dict() for g in self.groups]
        if self.sort is not None:
            d["sort"] = self.sort.to_dict()
        if self.filter is not None:
            d["filter"] = self.filter.to_dict()
        if self.tone_palettes:
            d["tone_palettes"] = [p.to_dict() for p in self.tone_palettes]
        return d


def format_view_model(vm: TuiViewModel) -> str:
    """Canonical, byte-identical compact JSON encoding of *vm*.

    See ``apcore-toolkit/docs/features/tui-view-model.md`` § Canonical JSON
    Encoding: declaration-order keys, optional fields omitted (never
    ``null``), lowercase booleans, no floating point, no whitespace.
    """
    return json.dumps(vm.to_dict(), ensure_ascii=False, separators=(",", ":"))


def _resolve_alias(module: ScannedModule, *, use_display: bool) -> str:
    if use_display and module.display:
        alias = module.display.get("alias")
        if alias:
            return str(alias)
    return module.module_id


def _resolve_description(module: ScannedModule, *, use_display: bool) -> str:
    if use_display and module.display:
        description = module.display.get("description")
        if description:
            return str(description)
    return module.description or ""


def _cell_for(column_key: str, module: ScannedModule, *, use_display: bool) -> Cell:
    if column_key == "module_id":
        return Cell(kind="text", value=module.module_id)
    if column_key == "alias":
        return Cell(kind="text", value=_resolve_alias(module, use_display=use_display))
    if column_key == "description":
        return Cell(kind="text", value=_resolve_description(module, use_display=use_display))
    if column_key == "tags":
        return Cell(kind="tags", values=list(module.tags or []))
    # Unknown/custom column key: fall back to an empty text cell rather than
    # raising, so a caller-declared column with no toolkit-known source still
    # yields a well-formed row (renderers may post-process).
    return Cell(kind="text", value="")


def _passes_filter(module: ScannedModule, flt: Filter | None, *, description: str) -> bool:
    if flt is None:
        return True
    module_tags = set(module.tags or [])
    if flt.tags and not set(flt.tags).issubset(module_tags):
        return False
    if flt.search:
        haystack = f"{module.module_id} {description}".lower()
        if flt.search.lower() not in haystack:
            return False
    annotations = module.annotations
    for annotation_name in flt.annotations:
        if annotation_name not in _FILTERABLE_ANNOTATION_FLAGS:
            return False
        if not bool(getattr(annotations, annotation_name, False)):
            return False
    # `discoverable` (ModuleAnnotations, default True) is the shipped signal
    # for "appears in enumeration surfaces" — hidden means not discoverable.
    is_hidden = not bool(getattr(annotations, "discoverable", True))
    if flt.exposure == "exposed" and is_hidden:
        return False
    if flt.exposure == "hidden" and not is_hidden:
        return False
    # ModuleAnnotations has no first-class `deprecated` field; the toolkit
    # convention (matching OpenAPIScanner) is `annotations.extra["deprecated"]`.
    is_deprecated = bool(getattr(annotations, "extra", {}).get("deprecated", False))
    if not flt.deprecated and is_deprecated:
        return False
    return True


def _sort_key(column_key: str, module: ScannedModule, *, alias: str, description: str) -> str:
    if column_key == "alias":
        return alias
    if column_key == "description":
        return description
    return module.module_id


def modules_to_view_model(
    modules: list[ScannedModule],
    *,
    view: View = "list",
    columns: tuple[str, ...] = (),
    title: str | None = None,
    filter: Filter | None = None,  # noqa: A002 - matches the documented public API name
    sort: Sort | None = None,
    group_by: GroupBy | None = None,
    tone_palettes: tuple[TonePalette, ...] = (),
    display: bool = True,
) -> TuiViewModel:
    """Build a byte-equivalent :class:`TuiViewModel` from scanned modules.

    See ``Contract: modules_to_view_model`` and the wire-format schema in
    ``apcore-toolkit/docs/features/tui-view-model.md``.

    Sort/filter execution model: filtering by ``tags`` / ``search`` /
    ``annotations`` / ``exposure`` / ``deprecated`` always executes here.
    Sorting by ``module_id`` / ``alias`` / ``description`` executes here;
    any other ``sort.key`` (e.g. usage-based ``calls`` / ``errors`` /
    ``latency``) is honoured verbatim in the incoming ``modules`` order —
    the caller is responsible for pre-sorting those.
    """
    # V1 convention: with no explicit per-column wiring in the public API,
    # the first supplied palette (if any) is referenced by the "tags"
    # column's `tone_by` — the only column shape a `tag_equals` rule can
    # meaningfully colour. Per-value tone resolution (which tag chip gets
    # which colour) is a Tier-2 renderer concern, not computed here.
    tags_palette = tone_palettes[0] if tone_palettes else None

    column_objs: list[Column] = []
    for key in columns:
        tone_by = tags_palette.name if (tags_palette is not None and key == "tags") else None
        column_objs.append(Column(key=key, label=_COLUMN_LABELS.get(key, key), tone_by=tone_by))

    resolved: list[tuple[ScannedModule, str, str]] = []
    for module in modules:
        alias = _resolve_alias(module, use_display=display)
        description = _resolve_description(module, use_display=display)
        if not _passes_filter(module, filter, description=description):
            continue
        resolved.append((module, alias, description))

    if sort is not None and sort.key in _SORTABLE_KEYS:
        reverse = sort.direction == "desc"
        resolved.sort(key=lambda entry: _sort_key(sort.key, entry[0], alias=entry[1], description=entry[2]), reverse=reverse)

    rows: list[Row] = []
    for module, alias, description in resolved:
        cells: list[Cell] = []
        for column in column_objs:
            if column.key == "alias":
                cell = Cell(kind="text", value=alias)
            elif column.key == "description":
                cell = Cell(kind="text", value=description)
            else:
                cell = _cell_for(column.key, module, use_display=display)
            cells.append(cell)
        rows.append(Row(cells=cells, tags=list(module.tags or [])))

    groups: list[Group] | None = None
    if view == "grouped":
        groups = _build_groups(resolved, group_by)

    return TuiViewModel(
        kind=view,
        title=title,
        columns=column_objs,
        rows=rows,
        groups=groups,
        sort=sort,
        filter=filter,
        tone_palettes=list(tone_palettes) if tone_palettes else None,
    )


def _build_groups(
    resolved: list[tuple[ScannedModule, str, str]],
    group_by: GroupBy | None,
) -> list[Group]:
    buckets: dict[str, list[int]] = {}
    order: list[str] = []
    for idx, (module, _alias, _description) in enumerate(resolved):
        labels: list[str]
        if group_by == "tag":
            labels = list(module.tags or []) or ["(untagged)"]
        elif group_by == "prefix":
            labels = [module.module_id.split(".", 1)[0]]
        else:
            labels = ["(all)"]
        for label in labels:
            if label not in buckets:
                buckets[label] = []
                order.append(label)
            buckets[label].append(idx)
    return [Group(label=label, row_indices=buckets[label]) for label in order]
