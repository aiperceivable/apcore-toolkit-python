# Feature Spec: Scanner Integration

**Status**: Draft
**Target**: apcore-toolkit-python v0.5.0
**Files**: `src/apcore_toolkit/scanner.py`, `src/apcore_toolkit/types.py`
**Tests**: `tests/test_scanner.py`, `tests/test_types.py`

---

## 1. Purpose

Expose the HTTP verb mapping functionality through two additive, backward-compatible changes:

1. Add `generate_suggested_alias()` as a `@staticmethod` on `BaseScanner`, matching the pattern of the existing `infer_annotations_from_method()`.
2. Add `suggested_alias: str | None = None` as a new optional field on the `ScannedModule` dataclass.

These changes let scanner subclasses call the toolkit's verb resolver through the base class interface and carry the resulting alias as a first-class module attribute.

---

## 2. Change 1: BaseScanner.generate_suggested_alias

### 2.1 Location

`src/apcore_toolkit/scanner.py`, added inside the `BaseScanner` class between `infer_annotations_from_method()` and `deduplicate_ids()`.

### 2.2 Method Signature

```python
@staticmethod
def generate_suggested_alias(path: str, method: str) -> str:
    """Generate a human-friendly suggested alias from HTTP route info.

    Convenience wrapper that delegates to
    :func:`apcore_toolkit.http_verb_map.generate_suggested_alias`.
    Provided so scanner subclasses can call the toolkit helper through
    the familiar ``BaseScanner`` interface.

    Examples:
        POST   /tasks/user_data       -> "tasks.user_data.create"
        GET    /tasks/user_data       -> "tasks.user_data.list"
        GET    /tasks/user_data/{id}  -> "tasks.user_data.get"

    Args:
        path: URL path (e.g., "/tasks/user_data/{id}").
        method: HTTP method (e.g., "POST"). Case-insensitive.

    Returns:
        Dot-separated alias string.
    """
    from apcore_toolkit.http_verb_map import generate_suggested_alias as _impl
    return _impl(path, method)
```

### 2.3 Implementation Notes

- **Local import**: `http_verb_map` is imported inside the method to avoid any potential circular dependency at module load and to keep the top-level `scanner.py` imports unchanged.
- **No state**: The method is `@staticmethod`, matching `infer_annotations_from_method`. It does not access `self` or `cls`.
- **No validation**: Delegates entirely to `http_verb_map.generate_suggested_alias`. Never raises.

### 2.4 Logic Steps

1. Import `generate_suggested_alias` from `apcore_toolkit.http_verb_map`.
2. Return the result of calling it with `(path, method)`.

### 2.5 Parameter Validation

Mirrors `http_verb_map.generate_suggested_alias`. No validation. Any string accepted.

### 2.6 Error Handling

No exceptions raised.

### 2.7 Verification Tests

Added to `tests/test_scanner.py` as a new test class `TestGenerateSuggestedAlias` following the existing pattern (see `TestInferAnnotationsFromMethod`).

```python
class TestGenerateSuggestedAlias:
    def test_post_collection(self) -> None:
        result = BaseScanner.generate_suggested_alias("/tasks/user_data", "POST")
        assert result == "tasks.user_data.create"

    def test_get_collection(self) -> None:
        result = BaseScanner.generate_suggested_alias("/tasks/user_data", "GET")
        assert result == "tasks.user_data.list"

    def test_get_single(self) -> None:
        result = BaseScanner.generate_suggested_alias("/tasks/user_data/{id}", "GET")
        assert result == "tasks.user_data.get"

    def test_delete_single(self) -> None:
        result = BaseScanner.generate_suggested_alias("/tasks/user_data/{id}", "DELETE")
        assert result == "tasks.user_data.delete"

    def test_case_insensitive_method(self) -> None:
        result = BaseScanner.generate_suggested_alias("/tasks", "post")
        assert result == "tasks.create"

    def test_root_path(self) -> None:
        result = BaseScanner.generate_suggested_alias("/", "GET")
        assert result == "list"

    def test_called_as_staticmethod(self) -> None:
        # Works without instantiation.
        assert BaseScanner.generate_suggested_alias("/users", "POST") == "users.create"

    def test_called_on_subclass(self) -> None:
        # Works via subclass access.
        result = ConcreteScanner.generate_suggested_alias("/users", "POST")
        assert result == "users.create"
```

| Test ID | Method | Coverage |
|---|---|---|
| T-SCN-SA-01 | `test_post_collection` | Basic POST delegation |
| T-SCN-SA-02 | `test_get_collection` | GET -> list |
| T-SCN-SA-03 | `test_get_single` | GET with path param -> get |
| T-SCN-SA-04 | `test_delete_single` | DELETE delegation |
| T-SCN-SA-05 | `test_case_insensitive_method` | Method case normalization |
| T-SCN-SA-06 | `test_root_path` | Root path edge case |
| T-SCN-SA-07 | `test_called_as_staticmethod` | Static invocation on base class |
| T-SCN-SA-08 | `test_called_on_subclass` | Static invocation on subclass |

---

## 3. Change 2: ScannedModule.suggested_alias

### 3.1 Location

`src/apcore_toolkit/types.py`, inside the `ScannedModule` dataclass.

### 3.2 Field Definition

```python
@dataclass
class ScannedModule:
    """Result of scanning a single endpoint.

    Attributes:
        module_id: Unique module identifier (e.g., 'users.get_user.get').
        description: Human-readable description for MCP tool listing.
        input_schema: JSON Schema dict for module input.
        output_schema: JSON Schema dict for module output.
        tags: Categorization tags.
        target: Callable reference in 'module.path:callable' format.
        version: Module version string.
        annotations: Behavioral annotations (readonly, destructive, etc.).
        documentation: Full docstring text for rich descriptions.
        suggested_alias: Scanner-generated human-friendly alias used by
            surface adapters in the resolve chain before falling back to
            module_id. Scanners SHOULD set this using
            ``BaseScanner.generate_suggested_alias()`` when the source
            endpoint has HTTP route information. Defaults to ``None``.
        examples: Example invocations for documentation and testing.
        metadata: Arbitrary key-value data (e.g., http_method, url_rule).
        warnings: Non-fatal issues encountered during scanning.
    """

    module_id: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    tags: list[str]
    target: str
    version: str = "1.0.0"
    annotations: ModuleAnnotations | None = None
    documentation: str | None = None
    suggested_alias: str | None = None
    examples: list[ModuleExample] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # Added in v0.5.0 (appended to avoid breaking positional construction
    # patterns established in earlier releases).
    display: dict[str, Any] | None = None
```

**`display` field**: A sparse display overlay persisted to binding YAML alongside `suggested_alias`. Holds per-surface overrides (alias, description, cli/mcp/a2a sections). Distinct from `metadata["display"]`, which holds the *resolved* form produced by `DisplayResolver` after the full resolve chain runs. Scanners may set this directly for static overrides; most callers leave it as `None` and let `DisplayResolver` populate `metadata["display"]` at resolve time.

### 3.3 Field Placement

- Inserted **after** `documentation` and **before** `examples`.
- All fields with default values remain grouped together at the end (dataclass field ordering requirement).
- This placement keeps presentation-related fields (`description`, `documentation`, `suggested_alias`) adjacent.

### 3.4 Default Value

`None`. This makes the field optional and ensures backward compatibility with:
- Existing tests that construct `ScannedModule(module_id=..., description=..., ...)` without `suggested_alias`.
- Existing scanners that have not yet been updated.

### 3.5 Semantic Rules

| Condition | Expected Value |
|---|---|
| HTTP-based scanner with known route | Non-empty string produced by `generate_suggested_alias()` |
| Non-HTTP scanner (e.g., ConventionScanner) | `None` (no natural alias) |
| Scanner unable to derive alias | `None` |

`None` and empty string are treated equivalently by the `DisplayResolver` (falsy check).

### 3.6 Verification Tests

Added to `tests/test_types.py` as a new test class `TestSuggestedAlias`.

```python
class TestSuggestedAlias:
    def _make_base_module(self, **overrides: Any) -> ScannedModule:
        defaults: dict[str, Any] = {
            "module_id": "tasks.user_data.post",
            "description": "Create task data",
            "input_schema": {"type": "object", "properties": {}},
            "output_schema": {"type": "object", "properties": {}},
            "tags": [],
            "target": "mod:func",
        }
        defaults.update(overrides)
        return ScannedModule(**defaults)

    def test_default_is_none(self) -> None:
        mod = self._make_base_module()
        assert mod.suggested_alias is None

    def test_set_via_constructor(self) -> None:
        mod = self._make_base_module(suggested_alias="tasks.user_data.create")
        assert mod.suggested_alias == "tasks.user_data.create"

    def test_explicit_none(self) -> None:
        mod = self._make_base_module(suggested_alias=None)
        assert mod.suggested_alias is None

    def test_field_is_independent_of_metadata(self) -> None:
        mod = self._make_base_module(
            suggested_alias="field_value",
            metadata={"suggested_alias": "metadata_value"},
        )
        assert mod.suggested_alias == "field_value"
        assert mod.metadata["suggested_alias"] == "metadata_value"

    def test_dataclasses_replace_preserves_field(self) -> None:
        from dataclasses import replace
        mod = self._make_base_module(suggested_alias="tasks.create")
        new_mod = replace(mod, module_id="tasks.create_v2")
        assert new_mod.suggested_alias == "tasks.create"
```

| Test ID | Method | Coverage |
|---|---|---|
| T-TYP-SA-01 | `test_default_is_none` | Default value |
| T-TYP-SA-02 | `test_set_via_constructor` | Constructor assignment |
| T-TYP-SA-03 | `test_explicit_none` | Explicit None passes through |
| T-TYP-SA-04 | `test_field_is_independent_of_metadata` | Field and metadata coexist |
| T-TYP-SA-05 | `test_dataclasses_replace_preserves_field` | `replace()` compatibility |

---

## 4. Public API Re-Exports

### 4.1 Update to __init__.py

```python
# src/apcore_toolkit/__init__.py

from apcore_toolkit.http_verb_map import (
    SCANNER_VERB_MAP,
    generate_suggested_alias,
    has_path_params,
    resolve_http_verb,
)
# ... existing imports ...

__all__ = [
    # ... existing entries alphabetically ...
    "SCANNER_VERB_MAP",
    # ... existing entries ...
    "generate_suggested_alias",
    "has_path_params",
    "resolve_http_verb",
    # ... existing entries ...
]
```

New symbols MUST be added to `__all__` in the correct alphabetical position to match the existing sorted style.

### 4.2 Verification

A basic import smoke test confirms the re-exports:

```python
class TestPublicApiReexports:
    def test_import_scanner_verb_map(self) -> None:
        from apcore_toolkit import SCANNER_VERB_MAP
        assert isinstance(SCANNER_VERB_MAP, dict)
        assert SCANNER_VERB_MAP["POST"] == "create"

    def test_import_resolve_http_verb(self) -> None:
        from apcore_toolkit import resolve_http_verb
        assert resolve_http_verb("POST", False) == "create"

    def test_import_has_path_params(self) -> None:
        from apcore_toolkit import has_path_params
        assert has_path_params("/tasks/{id}") is True

    def test_import_generate_suggested_alias(self) -> None:
        from apcore_toolkit import generate_suggested_alias
        assert generate_suggested_alias("/tasks", "POST") == "tasks.create"
```

These tests live at the top of `tests/test_http_verb_map.py` for locality.

| Test ID | Method | Coverage |
|---|---|---|
| T-API-RX-01 | `test_import_scanner_verb_map` | Constant re-export |
| T-API-RX-02 | `test_import_resolve_http_verb` | Function re-export |
| T-API-RX-03 | `test_import_has_path_params` | Function re-export |
| T-API-RX-04 | `test_import_generate_suggested_alias` | Function re-export |

---

## 5. Backward Compatibility

### 5.1 ScannedModule Construction

All existing call sites constructing `ScannedModule` keep working because `suggested_alias` has a default of `None`.

### 5.2 Serialization

The existing serializers in `apcore_toolkit.serializers` produce dictionaries via field iteration. The new field will appear in serialized output when set; when `None`, downstream consumers should skip it in the same way they skip other `None`-valued fields.

Any serializer update (if needed to emit `suggested_alias` only when non-`None`) is covered by the regression tests in `tests/test_serializers.py` and is considered part of this work item only if existing tests fail. No changes are anticipated.

### 5.3 BaseScanner Subclasses

Existing subclasses are unaffected. The new `@staticmethod` does not override or alter any existing behavior and does not require implementation by subclasses.

---

## 6. Acceptance Criteria

- [ ] `BaseScanner.generate_suggested_alias` added as `@staticmethod` exactly as specified.
- [ ] `ScannedModule.suggested_alias` field added with default `None`.
- [ ] Public API re-exports updated in `__init__.py` with correct alphabetical ordering.
- [ ] All tests in sections 2.7, 3.6, and 4.2 pass.
- [ ] Existing tests in `test_scanner.py`, `test_types.py`, `test_serializers.py` continue to pass without modification.
- [ ] `ruff check` and `mypy --strict` pass for all touched files.
