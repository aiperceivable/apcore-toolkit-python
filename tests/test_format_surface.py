"""Tests for surface-aware formatters: format_schema / format_module / format_modules."""

from __future__ import annotations


import pytest
from apcore import ModuleAnnotations

from apcore_toolkit import (
    ScannedModule,
    format_module,
    format_modules,
    format_schema,
)


def _fixture_module(**overrides) -> ScannedModule:
    base = dict(
        module_id="users.get_user",
        description="Look up a user by id",
        input_schema={
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "User id"},
            },
            "required": ["id"],
        },
        output_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
        },
        tags=["users"],
        target="myapp.views:get_user",
        annotations=ModuleAnnotations(readonly=True, cacheable=True),
    )
    base.update(overrides)
    return ScannedModule(**base)


class TestFormatSchema:
    def test_prose_required_and_optional(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "User id"},
                "verbose": {"type": "boolean"},
            },
            "required": ["id"],
        }
        out = format_schema(schema, style="prose")
        assert "`id` (integer, required) — User id" in out
        assert "`verbose` (boolean, optional)" in out

    def test_table_columns(self) -> None:
        schema = {
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "User id"}},
            "required": ["id"],
        }
        out = format_schema(schema, style="table")
        assert "| Name | Type | Required | Default | Description |" in out
        assert "| `id` | integer | yes |  | User id |" in out

    def test_json_passthrough(self) -> None:
        schema = {"type": "object", "properties": {"id": {"type": "integer"}}}
        out = format_schema(schema, style="json")
        assert out is schema

    def test_unknown_style_raises(self) -> None:
        with pytest.raises(ValueError):
            format_schema({}, style="bogus")  # type: ignore[arg-type]

    def test_max_depth_collapses_nested(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "outer": {
                    "type": "object",
                    "properties": {
                        "inner": {
                            "type": "object",
                            "properties": {"deep": {"type": "string"}},
                        },
                    },
                }
            },
        }
        out = format_schema(schema, style="prose", max_depth=2)
        # The inner object should collapse to a JSON code fence somewhere.
        assert "```json" in out

    def test_non_object_schema(self) -> None:
        out = format_schema({"type": "string"}, style="prose")
        assert "string" in out

    def test_empty_schema_prose(self) -> None:
        assert format_schema({}, style="prose") == ""


class TestFormatModuleMarkdown:
    def test_includes_title_description_parameters(self) -> None:
        out = format_module(_fixture_module(), style="markdown")
        assert isinstance(out, str)
        assert out.startswith("# users.get_user")
        assert "Look up a user by id" in out
        assert "## Parameters" in out
        assert "## Returns" in out
        assert "`id` (integer, required) — User id" in out

    def test_annotations_table_under_behavior(self) -> None:
        out = format_module(_fixture_module(), style="markdown")
        assert "## Behavior" in out
        assert "| Flag | Value |" in out
        assert "`readonly`" in out
        assert "`cacheable`" in out
        # destructive=False matches the default, so it must not appear.
        assert "`destructive`" not in out

    def test_annotations_bools_lowercase(self) -> None:
        out = format_module(_fixture_module(), style="markdown")
        assert "| `readonly` | true |" in out
        assert "| `cacheable` | true |" in out
        # Python's str(True) == "True" must NOT leak through.
        assert "| `readonly` | True |" not in out

    def test_annotations_rows_alphabetical(self) -> None:
        out = format_module(_fixture_module(), style="markdown")
        readonly_idx = out.index("`readonly`")
        cacheable_idx = out.index("`cacheable`")
        # 'cacheable' < 'readonly' alphabetically
        assert cacheable_idx < readonly_idx

    def test_annotations_skips_default_values(self) -> None:
        """`pagination_style` defaults to 'cursor'; default-value rows are skipped."""
        out = format_module(_fixture_module(), style="markdown")
        assert "`pagination_style`" not in out

    def test_behavior_section_omitted_when_all_default(self) -> None:
        from apcore import ModuleAnnotations as _Annotations

        module = _fixture_module(annotations=_Annotations())
        out = format_module(module, style="markdown")
        assert "## Behavior" not in out

    def test_behavior_section_omitted_when_annotations_none(self) -> None:
        module = _fixture_module(annotations=None)
        out = format_module(module, style="markdown")
        assert "## Behavior" not in out

    def test_examples_emitted_when_present(self) -> None:
        from apcore import ModuleExample

        module = _fixture_module(examples=[ModuleExample(title="lookup", inputs={"id": 1}, output={"name": "Ada"})])
        out = format_module(module, style="markdown")
        assert "## Examples" in out
        assert "Ada" in out

    def test_tags_section(self) -> None:
        out = format_module(_fixture_module(), style="markdown")
        assert "## Tags" in out
        assert "`users`" in out


class TestFormatModuleSkill:
    def test_minimal_frontmatter(self) -> None:
        out = format_module(_fixture_module(), style="skill")
        assert isinstance(out, str)
        assert out.startswith("---\n")
        first_block, _, _ = out.partition("\n---\n")
        # Only `name` and `description` keys are emitted.
        assert "name: users.get_user" in first_block
        assert "description: " in first_block
        # No vendor-specific keys.
        for forbidden in ("allowed-tools", "paths", "when_to_use", "user-invocable"):
            assert forbidden not in out

    def test_skill_body_matches_markdown(self) -> None:
        skill = format_module(_fixture_module(), style="skill")
        markdown = format_module(_fixture_module(), style="markdown")
        # Strip the YAML frontmatter and confirm the body is identical.
        body = skill.split("\n---\n", 1)[1].lstrip("\n")
        assert body == markdown


class TestFormatModuleTableRow:
    def test_pipe_separated(self) -> None:
        out = format_module(_fixture_module(), style="table-row")
        assert isinstance(out, str)
        assert "`users.get_user`" in out
        assert "Look up a user by id" in out
        assert "users" in out  # tag


class TestFormatModuleJson:
    def test_passthrough_dict(self) -> None:
        out = format_module(_fixture_module(), style="json")
        assert isinstance(out, dict)
        assert out["module_id"] == "users.get_user"
        assert out["description"] == "Look up a user by id"


class TestDisplayOverlay:
    def test_display_true_uses_overlay(self) -> None:
        module = _fixture_module(
            display={
                "alias": "lookup-user",
                "description": "Quickly look someone up.",
                "tags": ["accounts"],
            }
        )
        out = format_module(module, style="markdown", display=True)
        assert "# lookup-user" in out
        assert "Quickly look someone up." in out
        assert "`accounts`" in out

    def test_display_false_uses_raw(self) -> None:
        module = _fixture_module(display={"alias": "lookup-user", "description": "ignored"})
        out = format_module(module, style="markdown", display=False)
        assert "# users.get_user" in out
        assert "Look up a user by id" in out
        assert "lookup-user" not in out


class TestFormatModuleErrors:
    def test_unknown_style(self) -> None:
        with pytest.raises(ValueError):
            format_module(_fixture_module(), style="bogus")  # type: ignore[arg-type]


class TestFormatModules:
    def test_ungrouped_concatenates(self) -> None:
        modules = [
            _fixture_module(),
            _fixture_module(module_id="users.create_user", description="Create a user", tags=["users"]),
        ]
        out = format_modules(modules, style="markdown")
        assert "users.get_user" in out
        assert "users.create_user" in out

    def test_group_by_tag(self) -> None:
        modules = [
            _fixture_module(),
            _fixture_module(module_id="tasks.list", description="List tasks", tags=["tasks"]),
        ]
        out = format_modules(modules, style="markdown", group_by="tag")
        assert "## users" in out
        assert "## tasks" in out

    def test_group_by_prefix(self) -> None:
        modules = [
            _fixture_module(),
            _fixture_module(module_id="tasks.list", description="List tasks", tags=[]),
        ]
        out = format_modules(modules, style="markdown", group_by="prefix")
        assert "## users" in out
        assert "## tasks" in out

    def test_json_returns_list_of_dicts(self) -> None:
        modules = [_fixture_module()]
        out = format_modules(modules, style="json")
        assert isinstance(out, list)
        assert out[0]["module_id"] == "users.get_user"

    def test_unknown_group_by(self) -> None:
        with pytest.raises(ValueError):
            format_modules([_fixture_module()], style="markdown", group_by="bogus")  # type: ignore[arg-type]

    def test_untagged_bucket_when_group_by_tag(self) -> None:
        modules = [_fixture_module(tags=[])]
        out = format_modules(modules, style="markdown", group_by="tag")
        assert "## (untagged)" in out


class TestSkillFrontmatterEscaping:
    def test_description_with_colon_is_quoted(self) -> None:
        module = _fixture_module(description="Get: by id")
        out = format_module(module, style="skill")
        # YAML colon in a scalar requires quoting.
        assert 'description: "Get: by id"' in out
