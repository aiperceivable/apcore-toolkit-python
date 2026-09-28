"""Tests for apcore_toolkit.binding_loader — BindingLoader."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml
from apcore import ModuleAnnotations

from apcore_toolkit import YAMLWriter
from apcore_toolkit.binding_loader import BindingLoader, BindingLoadError, _select_files
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

    def test_loose_mode_warns_and_coerces_wrong_type_optional_fields(
        self, loader: BindingLoader, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Loose-mode wrong-type policy (binding-loader.md § Loose-mode wrong-type policy):
        non-required fields with the wrong type warn and coerce to empty defaults
        instead of raising. Mirrors the Rust and TypeScript loaders.
        """
        entry = {
            "module_id": "x",
            "target": "pkg:func",
            "input_schema": 42,
            "output_schema": "not a dict",
            "tags": "single-string-not-a-list",
        }
        with caplog.at_level("WARNING", logger="apcore_toolkit"):
            modules = loader.load_data({"bindings": [entry]})
        assert len(modules) == 1
        assert modules[0].input_schema == {}
        assert modules[0].output_schema == {}
        assert modules[0].tags == []
        warning_messages = " ".join(r.message for r in caplog.records)
        assert "input_schema" in warning_messages
        assert "output_schema" in warning_messages
        assert "tags" in warning_messages

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

    def test_metadata_filters_proto_pollution_keys(
        self, loader: BindingLoader, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Regression: ``__proto__`` / ``constructor`` / ``prototype`` keys in
        binding-YAML metadata must be dropped at parse time so they cannot
        propagate to JS-side consumers (cross-language prototype-pollution
        guard). Mirrors TypeScript ``binding-parser.ts`` ``PROTO_DENY``."""
        import logging

        entry = {
            "module_id": "x",
            "target": "p:f",
            "metadata": {
                "__proto__": {"polluted": True},
                "constructor": "evil",
                "prototype": ["bad"],
                "safe_key": "kept",
            },
        }
        with caplog.at_level(logging.WARNING, logger="apcore_toolkit"):
            m = loader.load_data({"bindings": [entry]})[0]
        assert m.metadata == {"safe_key": "kept"}
        assert "__proto__" in caplog.text
        assert "constructor" in caplog.text
        assert "prototype" in caplog.text

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
    """In strict mode, malformed non-required field types must raise
    BindingLoadError (not bare TypeError) and the error must carry the
    offending module_id. Loose mode warns and coerces — see
    ``test_loose_mode_warns_and_coerces_wrong_type_optional_fields``."""

    def test_input_schema_string_raises_binding_load_error(self, loader: BindingLoader) -> None:
        data = {"bindings": [{"module_id": "x", "target": "m:f", "input_schema": "not-a-dict"}]}
        with pytest.raises(BindingLoadError, match="input_schema"):
            loader.load_data(data, strict=True)

    def test_output_schema_int_raises_binding_load_error(self, loader: BindingLoader) -> None:
        data = {"bindings": [{"module_id": "x", "target": "m:f", "output_schema": 42}]}
        with pytest.raises(BindingLoadError, match="output_schema"):
            loader.load_data(data, strict=True)

    def test_tags_string_raises_binding_load_error(self, loader: BindingLoader) -> None:
        # Provide valid input_schema/output_schema so strict mode reaches the tags check.
        data = {
            "bindings": [
                {
                    "module_id": "x",
                    "target": "m:f",
                    "input_schema": {},
                    "output_schema": {},
                    "tags": "not-a-list",
                }
            ]
        }
        with pytest.raises(BindingLoadError, match="tags"):
            loader.load_data(data, strict=True)

    def test_error_carries_module_id(self, loader: BindingLoader) -> None:
        data = {"bindings": [{"module_id": "my.module", "target": "m:f", "input_schema": "oops"}]}
        with pytest.raises(BindingLoadError) as exc_info:
            loader.load_data(data, strict=True)
        assert exc_info.value.module_id == "my.module"


# ---------------------------------------------------------------------------
# D11-006: file size and file count limits
# ---------------------------------------------------------------------------
class TestBindingLoaderLimits:
    """D11-006: BindingLoader enforces 16 MiB file size and 10,000 file count."""

    def test_file_too_large_raises_binding_load_error(self, loader: BindingLoader, tmp_path: Path) -> None:
        """A file whose size exceeds _MAX_BINDING_FILE_SIZE must raise BindingLoadError."""
        from unittest.mock import patch
        from apcore_toolkit.binding_loader import _MAX_BINDING_FILE_SIZE

        # Write a minimal valid binding file
        binding_file = tmp_path / "big.binding.yaml"
        binding_file.write_text("spec_version: '1.0'\nbindings:\n  - module_id: x\n    target: m:f\n")

        # Patch Path.stat at the module level so only the size check is affected.
        # We call the real stat for is_file/is_dir checks (which use st_mode),
        # but return an oversized st_size for the size limit check.
        real_stat = Path.stat

        class FakeStat:
            st_mode = real_stat(binding_file).st_mode
            st_size = _MAX_BINDING_FILE_SIZE + 1

        def fake_stat(self, *args, **kwargs):  # noqa: N805
            return FakeStat()

        with patch.object(Path, "stat", fake_stat):
            with pytest.raises(BindingLoadError, match="(?i)too large|file.*large|large.*file"):
                loader.load(tmp_path)

    def test_too_many_files_raises_binding_load_error(self, loader: BindingLoader, tmp_path: Path) -> None:
        """More than _MAX_BINDING_FILES_PER_DIR files in a dir must raise BindingLoadError."""
        from unittest.mock import patch

        from apcore_toolkit import binding_loader as _bl
        from apcore_toolkit.binding_loader import _MAX_BINDING_FILES_PER_DIR

        # Create one real file so the directory exists and is valid
        (tmp_path / "real.binding.yaml").write_text(
            "spec_version: '1.0'\nbindings:\n  - module_id: x\n    target: m:f\n"
        )

        # Build a fake traversal result with too many entries. The cap is
        # applied to the *matched* set, so every fake name matches the
        # default pattern.
        fake_files = [tmp_path / f"fake_{i}.binding.yaml" for i in range(_MAX_BINDING_FILES_PER_DIR + 1)]

        with patch.object(_bl, "_iter_candidate_files", return_value=iter(fake_files)):
            with pytest.raises(BindingLoadError, match="(?i)too many"):
                loader.load(tmp_path)


# ---------------------------------------------------------------------------
# Pattern matching (docs/features/binding-loader.md § Pattern Matching)
# ---------------------------------------------------------------------------
class TestMatchName:
    """Unit tests for the shared name matcher backing ``load(pattern=...)``."""

    @pytest.mark.parametrize(
        ("pattern", "name", "expected"),
        [
            # '*' — zero or more characters, dots included
            ("*.binding.yaml", "users.binding.yaml", True),
            ("*.binding.yaml", "a.b.c.binding.yaml", True),
            ("*.binding.yaml", ".binding.yaml", True),
            ("*", "", True),
            ("*", "anything", True),
            ("**", "anything", True),
            ("*a*a*a*a*b", "a" * 64, False),  # bounded time, not exponential
            # '?' — exactly one character
            ("?", "a", True),
            ("?", "", False),
            ("?", "ab", False),
            ("a?c", "a.c", True),
            # anchoring — a pattern is matched whole, never as a substring
            ("users.binding.yaml", "users.binding.yaml", True),
            ("users.binding.yaml", "old-users.binding.yaml", False),
            (".binding.yaml", "users.binding.yaml", False),
            # metacharacters that are NOT supported are literals
            ("[ab].binding.yaml", "a.binding.yaml", False),
            ("[ab].binding.yaml", "[ab].binding.yaml", True),
            ("{a,b}.binding.yaml", "a.binding.yaml", False),
            ("{a,b}.binding.yaml", "{a,b}.binding.yaml", True),
            ("[!x].yaml", "a.yaml", False),
            ("[^x].yaml", "a.yaml", False),
            ("a-b.yaml", "a-b.yaml", True),
            # case sensitivity, on every platform
            ("*.binding.yaml", "users.BINDING.yaml", False),
            ("API-*.yaml", "api-v1.yaml", False),
            # backtracking across several stars
            ("*-*.binding.yaml", "api-v1.binding.yaml", True),
            ("*-*.binding.yaml", "apiv1.binding.yaml", False),
            ("a*b*c", "abc", True),
            ("a*b*c", "aXXbYYc", True),
            ("a*b*c", "acb", False),
            # empty name
            ("", "", True),
            ("", "a", False),
        ],
    )
    def test_match_name(self, pattern: str, name: str, expected: bool) -> None:
        from apcore_toolkit.binding_loader import _match_name

        assert _match_name(pattern, name) is expected

    def test_match_is_over_code_points_not_bytes(self) -> None:
        """``?`` consumes one code point — not one UTF-8 byte, not one UTF-16 unit.

        The astral pair mirrors shared fixture cases 038/039: one ``?`` must
        match U+1F600 and two must not. Asserting only the positive direction
        would still pass an implementation that is wrong the other way.
        """
        from apcore_toolkit.binding_loader import _match_name

        assert _match_name("?.binding.yaml", "é.binding.yaml") is True
        assert _match_name("?.binding.yaml", "\U0001f600.binding.yaml") is True
        assert _match_name("??.binding.yaml", "\U0001f600.binding.yaml") is False
        assert _match_name("?", "\U0001f600") is True  # astral plane: one code point
        assert _match_name("??", "\U0001f600") is False

    def test_match_applies_no_unicode_normalization(self) -> None:
        """NFC and NFD spellings of the same grapheme are not equal here."""
        import unicodedata

        from apcore_toolkit.binding_loader import _match_name

        nfc = unicodedata.normalize("NFC", "é.binding.yaml")
        nfd = unicodedata.normalize("NFD", "é.binding.yaml")
        assert nfc != nfd
        assert _match_name(nfc, nfd) is False


class TestPatternIsNeverRejected:
    """Every string is a valid pattern; the loader never raises on one for
    syntactic reasons (apcore Algorithm A25 requirement 2, PROTOCOL_SPEC
    §5.12.6 clause 6). These cases were the inverse in a pre-release draft of 0.12.0."""

    @pytest.mark.parametrize(
        "pattern",
        ["", "sub/*.binding.yaml", "**/*.binding.yaml", "*.binding.yaml/", "/", "a[b", "{x,y}"],
    )
    def test_odd_pattern_yields_empty_not_an_error(self, loader: BindingLoader, tmp_path: Path, pattern: str) -> None:
        (tmp_path / "a.binding.yaml").write_text("spec_version: '1.0'\nbindings:\n  - module_id: x\n    target: m:f\n")
        assert loader.load(tmp_path, pattern=pattern) == []

    def test_backslash_is_a_literal_not_a_separator(self, loader: BindingLoader, tmp_path: Path) -> None:
        """A25 requirement 4 names ``\\`` a literal, so a filename containing
        one is matchable rather than a rejected pattern."""
        (tmp_path / "sub\\x.binding.yaml").write_text(
            "spec_version: '1.0'\nbindings:\n  - module_id: x\n    target: m:f\n"
        )
        modules = loader.load(tmp_path, pattern="sub\\*.binding.yaml")
        assert [m.module_id for m in modules] == ["x"]

    def test_missing_path_still_raises_for_the_path_not_the_pattern(
        self, loader: BindingLoader, tmp_path: Path
    ) -> None:
        """An odd pattern no longer pre-empts the real error."""
        with pytest.raises(BindingLoadError, match="(?i)does not exist"):
            loader.load(tmp_path / "nope", pattern="**/*.binding.yaml")

    def test_single_file_ignores_the_pattern_entirely(self, loader: BindingLoader, tmp_path: Path) -> None:
        f = tmp_path / "one.binding.yaml"
        f.write_text("spec_version: '1.0'\nbindings:\n  - module_id: x\n    target: m:f\n")
        modules = loader.load(f, pattern="**/*.binding.yaml")
        assert [m.module_id for m in modules] == ["x"]


class TestLoadPattern:
    """``pattern`` narrows which files are read; nothing else changes."""

    @staticmethod
    def _write(path: Path, module_id: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"spec_version: '1.0'\nbindings:\n  - module_id: {module_id}\n    target: m:f\n")

    def test_default_pattern_unchanged(self, loader: BindingLoader, tmp_path: Path) -> None:
        self._write(tmp_path / "a.binding.yaml", "a")
        self._write(tmp_path / "b.cli.yaml", "b")
        assert [m.module_id for m in loader.load(tmp_path)] == ["a"]

    def test_custom_pattern_selects_a_different_set(self, loader: BindingLoader, tmp_path: Path) -> None:
        self._write(tmp_path / "api-v1.cli.yaml", "api1")
        self._write(tmp_path / "web-v1.cli.yaml", "web1")
        self._write(tmp_path / "api-v1.binding.yaml", "apib")
        assert [m.module_id for m in loader.load(tmp_path, pattern="api-*.cli.yaml")] == ["api1"]

    def test_pattern_matches_names_at_every_depth(self, loader: BindingLoader, tmp_path: Path) -> None:
        self._write(tmp_path / "a.cli.yaml", "top")
        self._write(tmp_path / "nested" / "deep" / "b.cli.yaml", "deep")
        assert [m.module_id for m in loader.load(tmp_path, pattern="*.cli.yaml")] == ["top"]
        assert sorted(m.module_id for m in loader.load(tmp_path, recursive=True, pattern="*.cli.yaml")) == [
            "deep",
            "top",
        ]

    def test_directory_named_like_a_match_is_not_selected(self, loader: BindingLoader, tmp_path: Path) -> None:
        self._write(tmp_path / "api-a.cli.yaml", "a")
        self._write(tmp_path / "api-b.cli.yaml" / "inner.cli.yaml", "inner")
        loaded = loader.load(tmp_path, recursive=True, pattern="api-*.cli.yaml")
        assert [m.module_id for m in loaded] == ["a"]

    def test_pattern_ignored_for_a_single_file(self, loader: BindingLoader, tmp_path: Path) -> None:
        f = tmp_path / "odd-name.yaml"
        self._write(f, "odd")
        assert [m.module_id for m in loader.load(f, pattern="*.binding.yaml")] == ["odd"]

    def test_no_matches_returns_empty_list(self, loader: BindingLoader, tmp_path: Path) -> None:
        (tmp_path / "notes.md").write_text("# notes\n")
        assert loader.load(tmp_path) == []

    def test_bracket_pattern_is_literal_not_a_character_class(self, loader: BindingLoader, tmp_path: Path) -> None:
        """``Path.glob``/``fnmatch`` would treat this as a class; we must not."""
        self._write(tmp_path / "a.binding.yaml", "a")
        assert loader.load(tmp_path, pattern="[ab].binding.yaml") == []
        self._write(tmp_path / "[ab].binding.yaml", "literal")
        assert [m.module_id for m in loader.load(tmp_path, pattern="[ab].binding.yaml")] == ["literal"]

    def test_unreadable_root_directory_raises_binding_load_error(self, loader: BindingLoader, tmp_path: Path) -> None:
        """An OS error listing the root surfaces as BindingLoadError, not a bare OSError."""
        from unittest.mock import patch

        from apcore_toolkit import binding_loader as _bl

        def boom(root: Path, *, recursive: bool):  # type: ignore[no-untyped-def]
            raise PermissionError(13, "Permission denied")

        with patch.object(_bl, "_iter_candidate_files", boom):
            with pytest.raises(BindingLoadError, match="(?i)failed to list directory"):
                loader.load(tmp_path)

    def test_selection_sorted_by_code_point_case_sensitively(self, loader: BindingLoader, tmp_path: Path) -> None:
        """Uppercase sorts before lowercase, on every platform.

        Regression guard for the ``key=str`` in ``load``: ``sorted()`` over
        ``Path`` objects compares ``_str_normcase``, which case-folds on
        Windows and would return ``["a", "M", "z"]`` there. Mirrors shared
        fixture case ``binding_pattern_035_select_lexicographic_order``.
        """
        for name in ("z.binding.yaml", "a.binding.yaml", "M.binding.yaml"):
            self._write(tmp_path / name, name.split(".")[0])
        self._write(tmp_path / "nested" / "b.binding.yaml", "nested")

        loaded = loader.load(tmp_path, recursive=True)
        assert [m.module_id for m in loaded] == ["M", "a", "nested", "z"]

    def test_directory_named_like_a_match_is_not_selected_flat(self, loader: BindingLoader, tmp_path: Path) -> None:
        """Case 037's twin of 034: the file-type guard applies without ``recursive`` too.

        Filtering on name alone would select the directory here and then die at
        read time with EISDIR.
        """
        self._write(tmp_path / "a.binding.yaml", "a")
        (tmp_path / "b.binding.yaml").mkdir()
        (tmp_path / "b.binding.yaml" / "inner.txt").write_text("not a binding\n")

        assert [m.module_id for m in loader.load(tmp_path)] == ["a"]

    def test_symlink_to_file_is_followed_and_selected(self, loader: BindingLoader, tmp_path: Path) -> None:
        """The file-type check tests the TARGET, not the link.

        Dropping symlinked binding files would be a data-loss-shaped
        regression with no error. Mirrors shared fixture case 040.
        """
        self._write(tmp_path / "real.binding.yaml", "real")
        (tmp_path / "alias.binding.yaml").symlink_to(tmp_path / "real.binding.yaml")

        # Both are selected; both parse to the target file's single entry.
        assert [m.module_id for m in loader.load(tmp_path)] == ["real", "real"]
        assert [f.name for f in _select_files(tmp_path, recursive=False, pattern="*.binding.yaml")] == [
            "alias.binding.yaml",
            "real.binding.yaml",
        ]

    def test_symlinked_directory_is_neither_selected_nor_descended(self, loader: BindingLoader, tmp_path: Path) -> None:
        """Following file symlinks must not become following directory symlinks.

        The real directory is still traversed, so the inner file appears
        exactly once under its real path. Mirrors shared fixture case 041.
        """
        self._write(tmp_path / "a.binding.yaml", "a")
        self._write(tmp_path / "target" / "inner.binding.yaml", "inner")
        (tmp_path / "link.binding.yaml").symlink_to(tmp_path / "target", target_is_directory=True)

        assert [m.module_id for m in loader.load(tmp_path, recursive=True)] == ["a", "inner"]
        # Non-recursive too: a symlinked directory is not a file.
        assert [m.module_id for m in loader.load(tmp_path)] == ["a"]

    def test_broken_symlink_is_skipped(self, loader: BindingLoader, tmp_path: Path) -> None:
        """A dangling symlink is skipped like any other non-file, in both modes.

        Without the file-type guard on the recursive branch it would reach the
        reader and die with FileNotFoundError instead.
        """
        self._write(tmp_path / "a.binding.yaml", "a")
        (tmp_path / "dangling.binding.yaml").symlink_to(tmp_path / "nowhere.binding.yaml")

        assert [m.module_id for m in loader.load(tmp_path)] == ["a"]
        assert [m.module_id for m in loader.load(tmp_path, recursive=True)] == ["a"]
