"""Tests for apcore_toolkit.binding_loader — BindingLoader."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml
from apcore import ModuleAnnotations

from apcore_toolkit import YAMLWriter
from apcore_toolkit.binding_loader import BindingLoader, BindingLoadError
from apcore_toolkit.types import ScannedModule


@pytest.fixture
def loader() -> BindingLoader:
    return BindingLoader()


@pytest.fixture
def minimal_entry() -> dict:
    return {"module_id": "x.y", "target": "pkg:func"}


@pytest.fixture
def full_entry() -> dict:
    return {
        "module_id": "users.get_user",
        "target": "myapp.views:get_user",
        "description": "Get a user",
        "documentation": "Returns a user by ID.",
        "tags": ["users", "get"],
        "version": "2.0.0",
        "annotations": {"readonly": True, "cacheable": True, "cache_ttl": 60},
        "examples": [
            {"title": "happy", "inputs": {"id": 1}, "output": {"name": "alice"}},
        ],
        "metadata": {"http_method": "GET"},
        "input_schema": {"type": "object", "properties": {"id": {"type": "integer"}}},
        "output_schema": {"type": "object"},
        "display": {"mcp": {"alias": "users_get"}, "alias": "users.get"},
        "suggested_alias": "users.get.alt",
        "warnings": ["stale"],
    }


class TestLoadData:
    def test_loose_minimum_entry(self, loader: BindingLoader, minimal_entry: dict) -> None:
        modules = loader.load_data({"bindings": [minimal_entry]})
        assert len(modules) == 1
        m = modules[0]
        assert m.module_id == "x.y"
        assert m.target == "pkg:func"
        assert m.description == ""
        assert m.input_schema == {}
        assert m.output_schema == {}
        assert m.tags == []
        assert m.version == "1.0.0"
        assert m.annotations is None
        assert m.display is None

    def test_strict_requires_input_schema(self, loader: BindingLoader, minimal_entry: dict) -> None:
        with pytest.raises(BindingLoadError) as exc_info:
            loader.load_data({"bindings": [minimal_entry]}, strict=True)
        assert "input_schema" in exc_info.value.missing_fields
        assert "output_schema" in exc_info.value.missing_fields
        assert exc_info.value.module_id == "x.y"

    def test_strict_accepts_when_schemas_present(self, loader: BindingLoader) -> None:
        entry = {
            "module_id": "x.y",
            "target": "pkg:func",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
        }
        modules = loader.load_data({"bindings": [entry]}, strict=True)
        assert len(modules) == 1

    def test_missing_module_id_always_fails(self, loader: BindingLoader) -> None:
        with pytest.raises(BindingLoadError) as exc:
            loader.load_data({"bindings": [{"target": "pkg:func"}]})
        assert "module_id" in exc.value.missing_fields

    def test_missing_target_always_fails(self, loader: BindingLoader) -> None:
        with pytest.raises(BindingLoadError) as exc:
            loader.load_data({"bindings": [{"module_id": "x"}]})
        assert "target" in exc.value.missing_fields

    def test_wrong_type_module_id_rejected(self, loader: BindingLoader) -> None:
        """Regression: ``module_id: 42`` must NOT silently coerce to ``"42"``.

        Previously Python/TypeScript accepted non-string scalars and then
        coerced them via ``str()`` / ``String()``, while Rust rejected them.
        The same YAML must now behave identically across the three SDKs.
        """
        with pytest.raises(BindingLoadError) as exc:
            loader.load_data({"bindings": [{"module_id": 42, "target": "pkg:func"}]})
        assert "module_id" in exc.value.missing_fields

    def test_wrong_type_target_rejected(self, loader: BindingLoader) -> None:
        with pytest.raises(BindingLoadError) as exc:
            loader.load_data({"bindings": [{"module_id": "x", "target": True}]})
        assert "target" in exc.value.missing_fields

    def test_empty_string_module_id_rejected(self, loader: BindingLoader) -> None:
        """Empty strings count as missing — an empty identifier is never valid."""
        with pytest.raises(BindingLoadError) as exc:
            loader.load_data({"bindings": [{"module_id": "", "target": "pkg:func"}]})
        assert "module_id" in exc.value.missing_fields

    def test_strict_mode_rejects_non_object_input_schema(self, loader: BindingLoader) -> None:
        """In strict mode, ``input_schema`` must be a mapping (not a string)."""
        entry = {
            "module_id": "x",
            "target": "pkg:func",
            "input_schema": "not a dict",
            "output_schema": {"type": "object"},
        }
        with pytest.raises(BindingLoadError) as exc:
            loader.load_data({"bindings": [entry]}, strict=True)
        assert "input_schema" in exc.value.missing_fields

    def test_input_schema_deep_copied_on_load(self, loader: BindingLoader) -> None:
        """Regression: mutating a loaded module's input_schema must not leak back.

        Python previously did a shallow ``dict(raw_input_schema)``, so nested
        ``properties`` were shared with the parsed YAML source and downstream
        mutation corrupted the original data. Rust already deep-clones
        (``serde_json::Value.clone``); this brings Python in line.
        """
        source_schema = {"type": "object", "properties": {"id": {"type": "integer"}}}
        entry = {"module_id": "x", "target": "p:f", "input_schema": source_schema}
        m = loader.load_data({"bindings": [entry]})[0]
        m.input_schema["properties"]["id"]["type"] = "string"  # type: ignore[index]
        assert source_schema["properties"]["id"]["type"] == "integer"

    def test_metadata_deep_copied_on_load(self, loader: BindingLoader) -> None:
        source_meta = {"auth": {"scope": ["admin", "write"]}}
        entry = {"module_id": "x", "target": "p:f", "metadata": source_meta}
        m = loader.load_data({"bindings": [entry]})[0]
        m.metadata["auth"]["scope"].append("leaked")
        assert source_meta["auth"]["scope"] == ["admin", "write"]

    def test_missing_bindings_key(self, loader: BindingLoader) -> None:
        with pytest.raises(BindingLoadError, match="bindings"):
            loader.load_data({"spec_version": "1.0"})

    def test_bindings_not_a_list(self, loader: BindingLoader) -> None:
        with pytest.raises(BindingLoadError, match="not a list"):
            loader.load_data({"bindings": "nope"})  # type: ignore[arg-type]

    def test_entry_not_a_mapping(self, loader: BindingLoader) -> None:
        with pytest.raises(BindingLoadError, match="mapping"):
            loader.load_data({"bindings": ["scalar"]})  # type: ignore[list-item]

    def test_top_level_not_mapping(self, loader: BindingLoader) -> None:
        with pytest.raises(BindingLoadError, match="mapping"):
            loader.load_data(["a", "b"])  # type: ignore[arg-type]

    def test_annotations_parsed(self, loader: BindingLoader, full_entry: dict) -> None:
        m = loader.load_data({"bindings": [full_entry]})[0]
        assert isinstance(m.annotations, ModuleAnnotations)
        assert m.annotations.readonly is True
        assert m.annotations.cacheable is True
        assert m.annotations.cache_ttl == 60

    def test_annotations_wrong_type_logs_warning(self, loader: BindingLoader, caplog: pytest.LogCaptureFixture) -> None:
        entry = {"module_id": "x", "target": "p:f", "annotations": "readonly"}
        with caplog.at_level(logging.WARNING, logger="apcore_toolkit"):
            m = loader.load_data({"bindings": [entry]})[0]
        assert m.annotations is None
        assert any("annotations" in r.message for r in caplog.records)

    def test_display_preserved(self, loader: BindingLoader, full_entry: dict) -> None:
        m = loader.load_data({"bindings": [full_entry]})[0]
        assert m.display == {"mcp": {"alias": "users_get"}, "alias": "users.get"}

    def test_display_absent_defaults_none(self, loader: BindingLoader, minimal_entry: dict) -> None:
        m = loader.load_data({"bindings": [minimal_entry]})[0]
        assert m.display is None

    def test_display_wrong_type_logs_warning(self, loader: BindingLoader, caplog: pytest.LogCaptureFixture) -> None:
        """Malformed display (not a dict) is dropped — must warn, not silently ignore."""
        entry = {"module_id": "x", "target": "p:f", "display": "not-a-dict"}
        with caplog.at_level(logging.WARNING, logger="apcore_toolkit"):
            m = loader.load_data({"bindings": [entry]})[0]
        assert m.display is None
        assert any("display" in r.message and "x" in r.message for r in caplog.records)

    def test_display_deep_copied_from_source(self, loader: BindingLoader) -> None:
        """Mutating the returned display must not affect subsequent loads."""
        source = {"mcp": {"alias": "original"}}
        entry = {"module_id": "x", "target": "p:f", "display": source}
        m = loader.load_data({"bindings": [entry]})[0]
        assert m.display == {"mcp": {"alias": "original"}}
        m.display["mcp"]["alias"] = "mutated"  # type: ignore[index]
        assert source["mcp"]["alias"] == "original"

    def test_examples_parsed(self, loader: BindingLoader, full_entry: dict) -> None:
        m = loader.load_data({"bindings": [full_entry]})[0]
        assert len(m.examples) == 1
        assert m.examples[0].title == "happy"

    def test_examples_malformed_skipped(self, loader: BindingLoader, caplog: pytest.LogCaptureFixture) -> None:
        entry = {
            "module_id": "x",
            "target": "p:f",
            "examples": [{"title": "ok", "inputs": {}, "output": {}}, "bad", {"unknown_field": 1}],
        }
        with caplog.at_level(logging.WARNING, logger="apcore_toolkit"):
            m = loader.load_data({"bindings": [entry]})[0]
        assert len(m.examples) == 1
        assert m.examples[0].title == "ok"


class TestSpecVersion:
    def test_missing_spec_version_warns(self, loader: BindingLoader, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="apcore_toolkit"):
            loader.load_data({"bindings": [{"module_id": "x", "target": "p:f"}]})
        assert any("spec_version" in r.message for r in caplog.records)

    def test_unsupported_spec_version_warns(self, loader: BindingLoader, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="apcore_toolkit"):
            loader.load_data({"spec_version": "2.0", "bindings": [{"module_id": "x", "target": "p:f"}]})
        assert any("newer than supported" in r.message for r in caplog.records)

    def test_supported_spec_version_silent(self, loader: BindingLoader, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="apcore_toolkit"):
            loader.load_data({"spec_version": "1.0", "bindings": [{"module_id": "x", "target": "p:f"}]})
        assert not any("spec_version" in r.message for r in caplog.records)


class TestLoadFromFile:
    def test_single_file(self, loader: BindingLoader, tmp_path: Path, full_entry: dict) -> None:
        f = tmp_path / "one.binding.yaml"
        f.write_text(yaml.dump({"spec_version": "1.0", "bindings": [full_entry]}))
        modules = loader.load(f)
        assert len(modules) == 1
        assert modules[0].module_id == "users.get_user"

    def test_directory_loads_all_binding_files(self, loader: BindingLoader, tmp_path: Path) -> None:
        for i, name in enumerate(["a", "b", "c"]):
            (tmp_path / f"{name}.binding.yaml").write_text(
                yaml.dump(
                    {
                        "spec_version": "1.0",
                        "bindings": [{"module_id": name, "target": f"pkg:f{i}"}],
                    }
                )
            )
        (tmp_path / "unrelated.yaml").write_text("irrelevant: true")
        modules = loader.load(tmp_path)
        assert [m.module_id for m in modules] == ["a", "b", "c"]

    def test_nonexistent_path(self, loader: BindingLoader, tmp_path: Path) -> None:
        with pytest.raises(BindingLoadError, match="does not exist"):
            loader.load(tmp_path / "nope")

    def test_malformed_yaml(self, loader: BindingLoader, tmp_path: Path) -> None:
        f = tmp_path / "bad.binding.yaml"
        f.write_text("::: not yaml :::")
        with pytest.raises(BindingLoadError, match="parse YAML"):
            loader.load(f)

    def test_empty_file_skipped(self, loader: BindingLoader, tmp_path: Path) -> None:
        f = tmp_path / "empty.binding.yaml"
        f.write_text("")
        modules = loader.load(f)
        assert modules == []

    def test_recursive_glob_opt_in(self, loader: BindingLoader, tmp_path: Path) -> None:
        """Default load is flat; recursive=True descends into subdirs."""
        (tmp_path / "top.binding.yaml").write_text(
            yaml.dump({"spec_version": "1.0", "bindings": [{"module_id": "top", "target": "p:f"}]})
        )
        nested = tmp_path / "sub" / "deep"
        nested.mkdir(parents=True)
        (nested / "deep.binding.yaml").write_text(
            yaml.dump({"spec_version": "1.0", "bindings": [{"module_id": "deep", "target": "p:g"}]})
        )

        flat = loader.load(tmp_path)
        assert [m.module_id for m in flat] == ["top"]

        deep = loader.load(tmp_path, recursive=True)
        assert sorted(m.module_id for m in deep) == ["deep", "top"]

    def test_utf8_encoding_on_read(self, loader: BindingLoader, tmp_path: Path) -> None:
        """Non-ASCII aliases round-trip correctly regardless of platform locale."""
        f = tmp_path / "unicode.binding.yaml"
        f.write_bytes(
            yaml.dump(
                {
                    "spec_version": "1.0",
                    "bindings": [{"module_id": "g\u00f6tt", "target": "p:f"}],
                },
                allow_unicode=True,
            ).encode("utf-8")
        )
        m = loader.load(f)[0]
        assert m.module_id == "g\u00f6tt"

    def test_null_value_in_required_field_error_wording(self, loader: BindingLoader) -> None:
        """A present-but-null required field produces 'missing or invalid' wording.

        The wording widened from "missing or null" to "missing or invalid" in
        0.5.0 when the loader began rejecting wrong-type scalars (e.g.
        ``module_id: 42``) in addition to null/absent values — matching the
        Rust loader's ``MissingFields`` contract.
        """
        entry = {"module_id": "x", "target": None}
        with pytest.raises(BindingLoadError) as exc:
            loader.load_data({"bindings": [entry]})
        assert "missing or invalid" in exc.value.reason
        assert "target" in exc.value.missing_fields


class TestRoundTrip:
    def test_writer_loader_round_trip(self, tmp_path: Path) -> None:
        original = ScannedModule(
            module_id="round.trip",
            description="Round-trip test",
            input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
            output_schema={"type": "object"},
            tags=["demo"],
            target="demo.app:handler",
            version="1.2.3",
            annotations=ModuleAnnotations(readonly=True, streaming=True, cache_ttl=30),
            documentation="Docs here",
            metadata={"http_method": "GET"},
            display={"mcp": {"alias": "rt"}, "alias": "round-trip"},
        )
        YAMLWriter().write([original], str(tmp_path))
        loaded = BindingLoader().load(tmp_path)

        assert len(loaded) == 1
        m = loaded[0]
        assert m.module_id == original.module_id
        assert m.target == original.target
        assert m.description == original.description
        assert m.documentation == original.documentation
        assert m.tags == original.tags
        assert m.version == original.version
        assert m.input_schema == original.input_schema
        assert m.output_schema == original.output_schema
        assert m.metadata == original.metadata
        assert m.display == original.display
        assert m.annotations is not None
        assert m.annotations.readonly is True
        assert m.annotations.streaming is True
        assert m.annotations.cache_ttl == 30

    def test_suggested_alias_round_trip(self, tmp_path: Path) -> None:
        """Scanner-set suggested_alias must survive YAMLWriter → BindingLoader."""
        original = ScannedModule(
            module_id="tasks.user_data.post",
            description="Create task data",
            input_schema={"type": "object", "properties": {}},
            output_schema={"type": "object"},
            tags=[],
            target="demo.app:handler",
            suggested_alias="tasks.user_data.create",
        )
        YAMLWriter().write([original], str(tmp_path))
        loaded = BindingLoader().load(tmp_path)

        assert len(loaded) == 1
        assert loaded[0].suggested_alias == "tasks.user_data.create"

    def test_suggested_alias_none_round_trip(self, tmp_path: Path) -> None:
        """A module without suggested_alias must load back with None (not missing key crash)."""
        original = ScannedModule(
            module_id="tasks.noalias",
            description="",
            input_schema={},
            output_schema={},
            tags=[],
            target="demo.app:handler",
        )
        YAMLWriter().write([original], str(tmp_path))
        loaded = BindingLoader().load(tmp_path)
        assert loaded[0].suggested_alias is None


class TestMalformedFieldTypes:
    """Malformed field types must raise BindingLoadError, not bare TypeError."""

    def test_input_schema_string_raises_binding_load_error(self, loader: BindingLoader) -> None:
        data = {"bindings": [{"module_id": "x", "target": "m:f", "input_schema": "not-a-dict"}]}
        with pytest.raises(BindingLoadError, match="input_schema"):
            loader.load_data(data)

    def test_output_schema_int_raises_binding_load_error(self, loader: BindingLoader) -> None:
        data = {"bindings": [{"module_id": "x", "target": "m:f", "output_schema": 42}]}
        with pytest.raises(BindingLoadError, match="output_schema"):
            loader.load_data(data)

    def test_tags_string_raises_binding_load_error(self, loader: BindingLoader) -> None:
        data = {"bindings": [{"module_id": "x", "target": "m:f", "tags": "not-a-list"}]}
        with pytest.raises(BindingLoadError, match="tags"):
            loader.load_data(data)

    def test_error_carries_module_id(self, loader: BindingLoader) -> None:
        data = {"bindings": [{"module_id": "my.module", "target": "m:f", "input_schema": "oops"}]}
        with pytest.raises(BindingLoadError) as exc_info:
            loader.load_data(data)
        assert exc_info.value.module_id == "my.module"
