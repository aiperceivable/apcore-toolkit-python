"""Surface-aware formatters.

Render `ScannedModule` and JSON Schema for specific consumer surfaces:
LLM context (markdown), agent skill files (skill), CLI listings (table-row),
and programmatic APIs (json). See `apcore-toolkit/docs/features/formatting.md`.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any, Literal

from apcore import ModuleAnnotations

from apcore_toolkit.serializers import annotations_to_dict, module_to_dict
from apcore_toolkit.types import ScannedModule

# Snake-case dict of every default-valued annotation field. Used by the
# behavior-table renderer to skip fields that match the protocol default,
# keeping the table focused on what is actually non-default about the module.
_DEFAULT_ANNOTATIONS_DICT: dict[str, Any] = annotations_to_dict(ModuleAnnotations()) or {}

SchemaStyle = Literal["prose", "table", "json"]
ModuleStyle = Literal["markdown", "skill", "table-row", "json"]
GroupBy = Literal["tag", "prefix"]

_SCHEMA_STYLES: tuple[str, ...] = ("prose", "table", "json")
_MODULE_STYLES: tuple[str, ...] = ("markdown", "skill", "table-row", "json")
_GROUP_BY_VALUES: tuple[str, ...] = ("tag", "prefix")


def format_schema(
    schema: dict[str, Any],
    *,
    style: SchemaStyle = "prose",
    max_depth: int = 3,
) -> str | dict[str, Any]:
    """Render a JSON Schema for a specific surface.

    See ``Contract: format_schema`` in
    ``apcore-toolkit/docs/features/formatting.md``.
    """
    if style not in _SCHEMA_STYLES:
        raise ValueError(f"format_schema: unknown style {style!r}; expected one of {_SCHEMA_STYLES}")
    if style == "json":
        return schema

    if not isinstance(schema, dict) or schema.get("type") != "object" or not schema.get("properties"):
        # Non-object or empty schemas
        if not isinstance(schema, dict):
            return ""
        type_ = schema.get("type")
        if type_ and type_ != "object":
            return f"_schema accepts {type_}_"
        # object without properties
        return "" if style == "prose" else "| Name | Type | Required | Default | Description |\n|---|---|---|---|---|\n"

    properties: dict[str, Any] = schema["properties"]
    required: set[str] = set(schema.get("required", []))

    if style == "prose":
        return _format_schema_prose(properties, required, max_depth=max_depth)
    return _format_schema_table(properties, required, max_depth=max_depth)


def _format_schema_prose(
    properties: dict[str, Any],
    required: set[str],
    *,
    max_depth: int,
    depth: int = 0,
) -> str:
    lines: list[str] = []
    for name, prop in properties.items():
        type_ = prop.get("type", "any") if isinstance(prop, dict) else "any"
        req_label = "required" if name in required else "optional"
        desc = (prop.get("description", "") if isinstance(prop, dict) else "").strip()
        head = f"- `{name}` ({type_}, {req_label})"
        if desc:
            head += f" — {desc}"
        lines.append(head)
        if isinstance(prop, dict) and prop.get("type") == "object" and isinstance(prop.get("properties"), dict):
            if depth + 1 >= max_depth:
                lines.append("  ```json")
                for json_line in json.dumps(prop, indent=2).splitlines():
                    lines.append(f"  {json_line}")
                lines.append("  ```")
            else:
                nested_required = set(prop.get("required", []))
                nested = _format_schema_prose(
                    prop["properties"],
                    nested_required,
                    max_depth=max_depth,
                    depth=depth + 1,
                )
                for nested_line in nested.splitlines():
                    lines.append(f"  {nested_line}")
    return "\n".join(lines)


def _format_schema_table(
    properties: dict[str, Any],
    required: set[str],
    *,
    max_depth: int,
) -> str:
    rows: list[str] = [
        "| Name | Type | Required | Default | Description |",
        "|---|---|---|---|---|",
    ]
    for name, prop in properties.items():
        type_ = prop.get("type", "any") if isinstance(prop, dict) else "any"
        req_label = "yes" if name in required else "no"
        default = prop.get("default", "") if isinstance(prop, dict) else ""
        if isinstance(default, (dict, list)):
            default = json.dumps(default)
        desc = (prop.get("description", "") if isinstance(prop, dict) else "").strip()
        rows.append(f"| `{name}` | {type_} | {req_label} | {default} | {desc} |")
    return "\n".join(rows)


def format_module(
    module: ScannedModule,
    *,
    style: ModuleStyle = "markdown",
    display: bool = True,
) -> str | dict[str, Any]:
    """Render a single ``ScannedModule`` for the chosen surface.

    See ``Contract: format_module`` in
    ``apcore-toolkit/docs/features/formatting.md``.
    """
    if style not in _MODULE_STYLES:
        raise ValueError(f"format_module: unknown style {style!r}; expected one of {_MODULE_STYLES}")

    if style == "json":
        return module_to_dict(module)

    title, description, guidance, tags = _resolve_display_fields(module, use_display=display)

    if style == "table-row":
        alias = title if title != module.module_id else ""
        tag_str = ", ".join(tags) if tags else ""
        return f"`{module.module_id}` │ `{alias}` │ {description} │ {tag_str}"

    body = _render_module_markdown_body(
        module=module,
        title=title,
        description=description,
        guidance=guidance,
        tags=tags,
    )

    if style == "skill":
        # Vendor-neutral minimum: name + description only.
        # Escape any quote/colon in description for safe YAML.
        frontmatter_desc = description.replace("\n", " ").strip()
        frontmatter = f"---\nname: {title}\ndescription: {_yaml_scalar(frontmatter_desc)}\n---\n\n"
        return frontmatter + body

    return body


def _resolve_display_fields(
    module: ScannedModule,
    *,
    use_display: bool,
) -> tuple[str, str, str | None, list[str]]:
    """Return (title, description, guidance, tags) honouring display overlay."""
    raw_title = module.module_id
    raw_desc = module.description or ""
    raw_tags = list(module.tags) if module.tags else []
    if not use_display or not module.display:
        return raw_title, raw_desc, None, raw_tags

    overlay = module.display
    title = overlay.get("alias") or raw_title
    description = overlay.get("description") or raw_desc
    guidance = overlay.get("guidance")
    tags = overlay.get("tags") or raw_tags
    return title, description, guidance, list(tags)


def _render_module_markdown_body(
    *,
    module: ScannedModule,
    title: str,
    description: str,
    guidance: str | None,
    tags: list[str],
) -> str:
    sections: list[str] = []
    sections.append(f"# {title}")
    if description:
        sections.append(description)
    if guidance:
        sections.append(f"_{guidance}_")

    sections.append("## Parameters")
    params_prose = format_schema(module.input_schema or {}, style="prose")
    sections.append(params_prose if params_prose else "_(no parameters)_")

    sections.append("## Returns")
    returns_prose = format_schema(module.output_schema or {}, style="prose")
    sections.append(returns_prose if returns_prose else "_(no return schema)_")

    annotation_table = _render_annotations_table(module.annotations)
    if annotation_table:
        sections.append("## Behavior")
        sections.append(annotation_table)

    if module.examples:
        sections.append("## Examples")
        for idx, example in enumerate(module.examples, start=1):
            ex = dataclasses.asdict(example) if dataclasses.is_dataclass(example) else dict(example)
            sections.append(f"### Example {idx}")
            sections.append("```json")
            sections.append(json.dumps(ex, indent=2, ensure_ascii=False))
            sections.append("```")

    if tags:
        sections.append("## Tags")
        sections.append(", ".join(f"`{t}`" for t in tags))

    return "\n\n".join(sections) + "\n"


def _render_annotations_table(annotations: Any) -> str | None:
    """Render `ModuleAnnotations` as a Markdown fact table.

    Cross-SDK alignment rules (see
    `apcore-toolkit/docs/features/formatting.md` § Annotations Rendering):

    1. Emit only fields whose value differs from `ModuleAnnotations()` default.
    2. The `extra` free-form bag is always skipped.
    3. Rows are sorted alphabetically by snake_case key.
    4. Bool values render as lowercase `true` / `false`; everything else uses
       JSON serialisation for collections, `str()` for scalars.

    Returns `None` when the resulting table would be empty (i.e. every
    annotation field equals its default), causing the caller to omit the
    `## Behavior` section entirely.
    """
    data = annotations_to_dict(annotations)
    if not data:
        return None
    entries: list[tuple[str, Any]] = []
    for key, value in data.items():
        if key == "extra":
            continue
        if _DEFAULT_ANNOTATIONS_DICT.get(key) == value:
            continue
        entries.append((key, value))
    if not entries:
        return None
    entries.sort(key=lambda kv: kv[0])
    rows = ["| Flag | Value |", "|---|---|"]
    for key, value in entries:
        if value is True:
            rendered = "true"
        elif value is False:
            rendered = "false"
        elif isinstance(value, (list, dict)):
            rendered = json.dumps(value, ensure_ascii=False)
        else:
            rendered = str(value)
        rows.append(f"| `{key}` | {rendered} |")
    return "\n".join(rows)


def _yaml_scalar(text: str) -> str:
    """Quote a string for safe YAML scalar emission when needed."""
    if text == "":
        return '""'
    needs_quote = any(c in text for c in (":", "#", "{", "}", "[", "]", "'", '"', "\n", "&", "*", "!", "|", ">"))
    if not needs_quote and not text.startswith(("-", "?", "%", "@", "`")):
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def format_modules(
    modules: list[ScannedModule],
    *,
    style: ModuleStyle = "markdown",
    group_by: GroupBy | None = None,
    display: bool = True,
) -> str | list[dict[str, Any]]:
    """Render a sequence of ``ScannedModule`` for the chosen surface.

    See ``Contract: format_modules`` in
    ``apcore-toolkit/docs/features/formatting.md``.
    """
    if style not in _MODULE_STYLES:
        raise ValueError(f"format_modules: unknown style {style!r}; expected one of {_MODULE_STYLES}")
    if group_by is not None and group_by not in _GROUP_BY_VALUES:
        raise ValueError(f"format_modules: unknown group_by {group_by!r}; expected one of {_GROUP_BY_VALUES} or None")

    if style == "json":
        return [module_to_dict(m) for m in modules]

    if group_by is None:
        rendered = [format_module(m, style=style, display=display) for m in modules]
        joiner = "\n\n" if style in ("markdown", "skill") else "\n"
        return joiner.join(str(r) for r in rendered)

    groups = _group_modules(modules, group_by)
    out: list[str] = []
    for group_name, group_modules in groups.items():
        if style in ("markdown", "skill"):
            out.append(f"## {group_name}")
        else:
            out.append(f"── {group_name} ──")
        for m in group_modules:
            out.append(str(format_module(m, style=style, display=display)))
    joiner = "\n\n" if style in ("markdown", "skill") else "\n"
    return joiner.join(out)


def _group_modules(
    modules: list[ScannedModule],
    group_by: GroupBy,
) -> dict[str, list[ScannedModule]]:
    groups: dict[str, list[ScannedModule]] = {}
    for module in modules:
        if group_by == "prefix":
            prefix = module.module_id.split(".", 1)[0] if "." in module.module_id else module.module_id
            groups.setdefault(prefix, []).append(module)
        else:  # group_by == "tag"
            if not module.tags:
                groups.setdefault("(untagged)", []).append(module)
                continue
            for tag in module.tags:
                groups.setdefault(tag, []).append(module)
    return groups
