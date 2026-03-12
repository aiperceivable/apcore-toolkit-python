# Changelog

All notable changes to this project will be documented in this file.

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
