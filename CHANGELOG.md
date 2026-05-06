# Changelog

All notable changes to this project will be documented in this file.


## [0.6.0] - 2026-05-05

### Changed

- **`apcore` minimum version bumped from 0.19.0 to 0.20.0** — `pyproject.toml` `dependencies` now requires `apcore>=0.20.0`. Toolkit only imports stable apcore surface (`ModuleAnnotations`, `DEFAULT_ANNOTATIONS`, `ModuleExample`, `Registry`, `ErrorCodes`, `parse_docstring`, `errors.ModuleError`); none of these were affected by 0.20.0 changes. Full pytest suite (585 passed) verified against apcore 0.20.0.

### Added

- **Surface-aware formatters** (refs aiperceivable/apcore-toolkit#13) — `format_module`, `format_schema`, `format_modules` for rendering `ScannedModule` and JSON Schema for specific consumer surfaces. Four styles for `format_module`: `markdown` (LLM context), `skill` (drop-in `.claude/skills/<id>/SKILL.md` or `.gemini/skills/<id>/SKILL.md` body with minimal `name` + `description` frontmatter — no vendor-specific extensions), `table-row` (CLI listing), `json` (programmatic). `format_schema` styles: `prose`, `table`, `json`. `format_modules` adds optional `group_by="tag" | "prefix"`. `display=True` (default) prefers the `ScannedModule.display` overlay over raw fields. Lives in `apcore_toolkit.formatting.surface`; re-exported at the top-level package.
- **Annotation-table cross-SDK alignment** — `format_module(style="markdown" | "skill")` `## Behavior` table now emits only fields that differ from `ModuleAnnotations()` defaults, sorts rows alphabetically by snake_case key, and renders bool values as lowercase `true`/`false`. The section is omitted entirely when every annotation field matches its default. Closes the byte-equality gap with the TypeScript and Rust SDKs.

### Changed

- **`infer_annotations_from_method` canonical mapping** (refs aiperceivable/apcore-toolkit#11) — `HEAD` and `OPTIONS` now map to `readonly=true` (without `cacheable=true`), matching the canonical mapping declared in `apcore-toolkit/docs/features/scanning.md` and aligning with the existing Rust SDK. Previously these methods returned default annotations.

## [0.5.0] - 2026-04-21

### Added

- **`BindingLoader`** (`apcore_toolkit.binding_loader`) — parses `.binding.yaml` files back into `ScannedModule` objects, the inverse of `YAMLWriter`. Unlike `apcore.BindingLoader`, this is pure data: no target import, no Registry mutation. Enables verification, merging, diffing, and round-trip workflows.
  - `load(path, *, strict=False)` — single file or directory of `*.binding.yaml`.
  - `load_data(data, *, strict=False)` — pre-parsed YAML dict.
  - Loose mode (default): only `module_id + target` required; missing fields use defaults.
  - Strict mode: additionally requires `input_schema + output_schema`.
  - `spec_version` validated; missing/unsupported versions WARN but do not fail.
  - `annotations` parsed via `ModuleAnnotations.from_dict`; malformed values degrade to `None` with WARN.
  - `examples` entries validated individually; malformed ones skipped with WARN.
  - `BindingLoadError` exception carries `file_path`, `module_id`, `missing_fields`, `reason`.
- **`ScannedModule.display`** — new top-level optional field (`dict | None`) holding the sparse display overlay for binding YAML persistence. Distinct from `metadata["display"]` (resolved form produced by `DisplayResolver`).
- **New feature doc**: `docs/features/binding-loader.md`; `display-overlay.md` and `output-writers.md` updated.

### Changed

- **`YAMLWriter._build_binding`** — emits top-level `display:` key only when `ScannedModule.display is not None` (skip when None keeps output clean).
- **`serializers.module_to_dict`** — includes `display` key in output.
- **`AIEnhancer._build_prompt`** — confidence template is now built dynamically from `gaps`. When `annotations` is in gaps, the prompt requests per-field confidence for every `_ANNOTATION_FIELD_VALIDATORS` field (`annotations.readonly`, `annotations.streaming`, `annotations.cache_ttl`, ...). Previously the template hard-coded `{"description": 0.0, "documentation": 0.0}` only, causing all annotation-field confidence lookups to fall back to `0.0` and fail the threshold check — annotation enhancement silently never took effect. Fixes symmetry with `_enhance_module`'s `ann_conf.get(f"annotations.{field_name}", ...)` read path.

### Dependencies

- **`apcore >= 0.19.0`** — picks up the expanded `ModuleAnnotations` (12 fields incl. `streaming`, `cacheable`, `cache_ttl`, `cache_key_fields`, `paginated`, `pagination_style`, `extra`). No toolkit code changes were needed for the type itself — `_build_annotation_field_validators` reflects the updated dataclass automatically.

### Tests

- +34 new tests: 24 for `BindingLoader` (parsing, strict/loose modes, spec_version, file & directory loading, round-trip with `YAMLWriter`), 5 for the prompt confidence block, and 5 hardening tests (display deep-copy, malformed-shape warn, recursive glob, UTF-8 encoding, null-field error wording).
- Updated `test_field_count` (13 → 14) and `test_all_expected_keys` for the new `display` field.
- Total suite: 440 tests.

### Hardening (post-review)

- **`BindingLoader`**: warns (rather than silently drops) malformed `display` values that are not a mapping; `load()` gained a `recursive: bool = False` kwarg for nested binding layouts; `read_text` now forces UTF-8 decoding so non-ASCII aliases round-trip on non-UTF-8 locales; required-field validation now rejects wrong-type scalars (e.g. `module_id: 42`, `target: true`) and empty strings in addition to absent/null, matching the Rust loader's contract — error wording is "missing or invalid required fields"; nested `input_schema`/`output_schema`/`metadata` are now deep-copied via `copy.deepcopy` so caller mutation does not leak back into the parsed YAML source graph.
- **`YAMLWriter`**: `display` is now deep-copied into the emitted binding (defensive parity with the TypeScript/Rust writers) so post-write mutation of `ScannedModule.display` cannot leak into the file. File writes are now atomic: the payload is written to `<name>.<pid>.tmp`, `fsync`ed, then `os.replace`d onto the final path (matches the TypeScript `tmp + rename` and Rust `tmp + sync_all + rename` writers). A process crash mid-write no longer leaves a partial YAML file that `BindingLoader` would fail to parse. A pre-write check refuses to overwrite a symlink at the target path (defence-in-depth against TOCTOU).
- **`BaseScanner.deduplicate_ids`**: pre-scans all input `module_id`s so generated `_N` suffixes never collide with an ID already present in the input. Input `[a, a, a_2]` now yields `[a, a_3, a_2]` instead of the previous buggy `[a, a_2, a_2]`. Matches the TypeScript and Rust implementations.
- **`resolve_target` / `RegistryWriter.write`**: new `allowed_prefixes: list[str] | None` kwarg (forwarded from `RegistryWriter.write` through `_to_function_module` to `resolve_target`). When set, `resolve_target` rejects any module path outside the listed prefixes **before** calling `importlib.import_module`, raising `PermissionError`. Mitigates arbitrary-code-execution via forged binding files (e.g. a malicious `target: "os:system"` injected into untrusted YAML). Parity with the TypeScript SDK's `allowedPrefixes` option, adapted to Python's module-name import model. Boundary-aware: `"myapp"` permits `myapp.views` but NOT `myappx.foo`. Rust does not need this because `resolve_target` is parse-only and the `HandlerFactory` is the security boundary.
- **`ScannedModule.display`**: moved to the END of the dataclass so existing positional `ScannedModule(...)` callers are not broken by the new field.

## [0.4.1] - 2026-03-25

### Added

- **`deep_resolve_refs()`** — public API for recursive `$ref` resolution in OpenAPI schemas (previously internal `_deep_resolve_refs`). Resolves nested `allOf`/`anyOf`/`oneOf`, `items`, and `properties`. Depth-limited to 16 levels.

### Fixed

- README: apcore dependency version updated from `>= 0.13.1` to `>= 0.14.0` (matches pyproject.toml).
- README: Core Modules table now lists all public API functions (added 10 missing entries).

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
## [0.3.1] - 2026-03-22

### Changed
- Rebrand: aipartnerup → aiperceivable

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
