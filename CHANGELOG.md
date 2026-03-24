# Changelog

All notable changes to this project will be documented in this file.

## [0.4.0] - 2026-03-23

### Added

- **`DisplayResolver`** (`apcore_toolkit.display`) — sparse binding.yaml display overlay (§5.13). Merges per-surface presentation fields (alias, description, guidance, tags, documentation) into `ScannedModule.metadata["display"]` for downstream CLI/MCP/A2A surfaces.
  - Resolution chain per field: surface-specific override > `display` default > binding-level field > scanner value.
  - `resolve(modules, *, binding_path=..., binding_data=...)` — accepts pre-parsed dict or a path to a `.binding.yaml` file / directory of `*.binding.yaml` files. `binding_data` takes precedence over `binding_path`.
  - MCP alias auto-sanitization: replaces characters outside `[a-zA-Z0-9_-]` with `_`; prepends `_` if result starts with a digit.
  - MCP alias hard limit: raises `ValueError` if sanitized alias exceeds 64 characters.
  - CLI alias validation: warns and falls back to `display.alias` when user-explicitly-set alias does not match `^[a-z][a-z0-9_-]*$` (module_id fallback always accepted without warning).
  - `suggested_alias` in `ScannedModule.metadata` (emitted by `simplify_ids=True` scanner) used as fallback when no `display.alias` is set.
  - Match-count logging: `INFO` for match count, `WARNING` when binding map loaded but zero modules matched.
- **`DisplayResolver` public export** — available as `from apcore_toolkit.display import DisplayResolver` and `from apcore_toolkit import DisplayResolver`.

### Tests

- 30 new tests in `tests/test_display_resolver.py` covering: no-binding fallthrough, alias-only overlay, surface-specific overrides, MCP sanitization, MCP 64-char limit, `suggested_alias` fallback, sparse overlay (10 modules / 1 binding), tags resolution, `binding_path` file and directory loading, guidance chain, CLI invalid alias warning and fallback, `binding_data` vs `binding_path` precedence.

### Added (Convention Module Discovery — §5.14)

- **`ConventionScanner`** — scans a `commands/` directory of plain Python files for public functions and converts them to `ScannedModule` instances with schema inferred from type annotations.
  - Module ID: `{file_prefix}.{function_name}` with `MODULE_PREFIX` override.
  - Description from first line of docstring (`"(no description)"` fallback).
  - `input_schema` / `output_schema` inferred from PEP 484 type hints.
  - `CLI_GROUP` and `TAGS` module-level constants stored in metadata.
  - `include` / `exclude` regex filters on module IDs.
- **`ConventionScanner` public export** — available as `from apcore_toolkit import ConventionScanner`.

### Tests (Convention Module Discovery)

- 15 new tests in `tests/test_convention_scanner.py`.

---

## [0.3.0] - 2026-03-19

### Added

- `_deep_resolve_refs()` — recursive `$ref` resolution for nested OpenAPI schemas,
  handling `allOf`/`anyOf`/`oneOf`, `items`, and `properties`. Depth-limited to 16
  levels to prevent infinite recursion on circular references.
- `Enhancer` protocol — pluggable interface for metadata enhancement, allowing
  custom enhancers beyond the built-in `AIEnhancer`.
- `HTTPProxyRegistryWriter` — registers scanned modules as HTTP proxy classes
  that forward requests to a running web API. Supports path parameter substitution,
  pluggable auth headers, and `2xx` success range (with `204` returning `{}`).
  Requires optional `httpx` dependency (`pip install apcore-toolkit[http-proxy]`).
- `get_writer("http-proxy", base_url=...)` — factory support for the new
  HTTP proxy writer with `**kwargs` forwarding.
- Expanded `__init__.py` public API: exports `Enhancer`, `HTTPProxyRegistryWriter`,
  `WriteError`, `Verifier`, `VerifyResult`, verifier classes, serializer functions,
  `resolve_ref`, `resolve_schema`, `extract_input_schema`, `extract_output_schema`,
  and `run_verifier_chain`.

### Fixed

- `extract_output_schema()` — now recursively resolves all nested `$ref` pointers
  (previously only handled the shallow case of array items with `$ref`).
- `extract_input_schema()` — now recursively resolves `$ref` inside individual
  properties after assembly.
- `get_writer()` return type annotation now includes `HTTPProxyRegistryWriter`.

### Tests

- 272 tests (up from 260), all passing
- Added `TestDeepResolveRefs` (8 tests): top-level ref, nested properties,
  allOf/anyOf, array items, deeply nested refs, circular ref depth limit,
  immutability guarantee
- Added nested `$ref` tests for `extract_input_schema` and `extract_output_schema`
- Added `test_http_proxy` for `get_writer("http-proxy")` factory

---

## [0.2.0] - 2026-03-11

### Added

- `WriteResult` dataclass — structured result type for all writer operations,
  with optional output verification support
- `AIEnhancer` — SLM-based metadata enhancement using local OpenAI-compatible
  APIs (Ollama, vLLM, LM Studio). Fills missing descriptions, infers behavioral
  annotations, and generates input schemas for untyped functions. All AI-generated
  fields are tagged with `x-generated-by: slm` for auditability.
  Annotation inference prompt and acceptance logic extended to handle all 11
  annotation fields (5 new: `cacheable`, `cache_ttl`, `cache_key_fields`,
  `paginated`, `pagination_style`)
- Output verification via `verify=True` parameter on all writers:
  - `YAMLWriter`: validates YAML parsability and required binding fields
  - `PythonWriter`: validates Python syntax via `ast.parse()`
  - `RegistryWriter`: validates module is retrievable after registration
- CI matrix expanded to Python 3.11 + 3.12, added mypy and coverage reporting

### Changed

- **apcore >= 0.13.0** — Upgraded minimum dependency to support new
  `ModuleAnnotations` caching and pagination fields:
  `cacheable`, `cache_ttl`, `cache_key_fields`, `paginated`, `pagination_style`
- `infer_annotations_from_method()` — `GET` now also infers `cacheable=True`
- **BREAKING**: All writers now return `list[WriteResult]` instead of
  `list[dict]` (YAMLWriter) or `list[str]` (PythonWriter, RegistryWriter).
  Downstream adapters (`django-apcore`, `flask-apcore`) must update code that
  accesses writer return values as dicts or strings. Migration:
  - `result[0]["bindings"]` → use `writer._build_binding(module)` for dict access
  - `result == ["module.id"]` → `[r.module_id for r in result] == ["module.id"]`
  - `f"wrote: {item}"` → `f"wrote: {item.module_id}"`
  - Downstream packages should pin `apcore-toolkit<0.2.0` until updated

### Fixed

- README — added 5 concrete verifier classes (`YAMLVerifier`, `SyntaxVerifier`,
  `RegistryVerifier`, `MagicBytesVerifier`, `JSONVerifier`) to Core Modules table
  for documentation completeness

### Tests

- 197 tests (up from 150), all passing

---

## [0.1.0] - 2026-03-06

Initial release. Extracts shared framework-agnostic logic from `django-apcore`
and `flask-apcore` into a standalone toolkit package.

### Added

- `ScannedModule` dataclass — canonical representation of a scanned endpoint
- `BaseScanner` ABC with `filter_modules()`, `deduplicate_ids()`,
  `infer_annotations_from_method()`, and `extract_docstring()` utilities
- `YAMLWriter` — generates `.binding.yaml` files for `apcore.BindingLoader`
- `PythonWriter` — generates `@module`-decorated Python wrapper files
- `RegistryWriter` — registers modules directly into `apcore.Registry`
- `to_markdown()` — generic dict-to-Markdown conversion with depth control
  and table heuristics
- `flatten_pydantic_params()` — flattens Pydantic model parameters into
  scalar kwargs for MCP tool invocation
- `resolve_target()` — resolves `module.path:qualname` target strings
- `enrich_schema_descriptions()` — merges docstring parameter descriptions
  into JSON Schema properties
- `annotations_to_dict()` / `module_to_dict()` — serialization utilities
- OpenAPI utilities: `resolve_ref()`, `resolve_schema()`,
  `extract_input_schema()`, `extract_output_schema()`
- Output format factory via `get_writer()`
- 150 tests with 94% code coverage

### Dependencies

- apcore >= 0.9.0
- pydantic >= 2.0
- PyYAML >= 6.0
