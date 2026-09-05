<div align="center">
  <img src="https://raw.githubusercontent.com/aiperceivable/apcore-toolkit/main/apcore-toolkit-logo.svg" alt="apcore-toolkit logo" width="200"/>
</div>

# apcore-toolkit-python

Python implementation of the [apcore-toolkit](https://github.com/aiperceivable/apcore-toolkit).

Extracts ~1,400 lines of duplicated framework-agnostic logic from `django-apcore` and `flask-apcore` into a standalone Python package.

## Installation

```bash
pip install apcore-toolkit
```


## Core Modules

| Module | Description |
|--------|-------------|
| `ScannedModule` | Canonical dataclass representing a scanned endpoint |
| `create_scanned_module` | Factory that constructs a `ScannedModule` with sensible defaults |
| `clone_module` | Returns a copy of a `ScannedModule` with selected fields overridden |
| `BaseScanner` | Abstract base class for framework scanners with filtering and deduplication |
| `filter_modules` | Standalone include/exclude regex filter over a list of `ScannedModule` |
| `deduplicate_ids` | Removes modules with duplicate `module_id`, keeping first occurrence |
| `infer_annotations_from_method` | Infers `ModuleAnnotations` from an HTTP method string (GET/POST/etc.) |
| `YAMLWriter` | Generates `.binding.yaml` files for `apcore.BindingLoader` |
| `BindingLoader` | Parses `.binding.yaml` files back into `ScannedModule` objects (pure-data inverse of `YAMLWriter`, with loose/strict modes) |
| `BindingLoadError` | Exception raised when binding parsing fails; carries `file_path`, `module_id`, `missing_fields`, `reason` |
| `PythonWriter` | Generates `@module`-decorated Python wrapper files |
| `RegistryWriter` | Registers modules directly into an `apcore.Registry`. Subclasses customize only the `_adapt_func` / `_build_input_schema` / `_build_output_schema` hooks; field mapping (incl. `annotations`) is centralized so no override can silently drop a field |
| `assert_annotations_preserved` | Conformance check for adapter test suites: registers a module and asserts its behavioral annotations (`requires_approval` / `destructive`) survive `get_definition` — guards against a writer silently disabling approval/ACL gating |
| `HTTPProxyRegistryWriter` | Registers HTTP proxy modules that forward requests to a running API (requires `pip install apcore-toolkit[http-proxy]`) |
| `Enhancer` | Pluggable protocol for metadata enhancement |
| `AIEnhancer` | SLM-based metadata enhancement for scanned modules |
| `WriteResult` | Structured result type for all writer operations |
| `WriteError` | Error class for I/O failures during write |
| `InvalidFormatError` | Raised by `get_writer` when an unknown output format name is requested |
| `Verifier` | Pluggable protocol for validating written artifacts |
| `VerifyResult` | Result type for verification operations |
| `YAMLVerifier` | Verifies YAML files parse correctly with required fields |
| `SyntaxVerifier` | Verifies source files are non-empty and readable |
| `RegistryVerifier` | Verifies modules are registered and retrievable |
| `MagicBytesVerifier` | Verifies file headers match expected magic bytes |
| `JSONVerifier` | Verifies JSON files parse correctly; accepts an optional `schema: dict` constructor arg to additionally validate against a JSON Schema (requires `pip install apcore-toolkit[json-schema]`) |
| `to_markdown` | Converts arbitrary dicts to Markdown with depth control and table heuristics |
| `format_module` _(v0.6.0)_ | Surface-aware renderer for a single `ScannedModule` (styles: `markdown`, `skill`, `table-row`, `json`) |
| `format_modules` _(v0.6.0)_ | Batch renderer for a list of `ScannedModule`; supports grouping by tag or module-id prefix |
| `format_schema` _(v0.6.0)_ | Surface-aware JSON Schema renderer (styles: `prose`, `table`, `json`) with depth control |
| `format_csv` _(v0.7.0)_ | Byte-equivalent RFC 4180 CSV emitter — header = union of keys across all rows; nested cells = canonical JSON; CRLF terminator |
| `format_jsonl` _(v0.7.0)_ | Byte-equivalent JSON Lines emitter — canonical compact JSON per row, LF terminator |
| `flatten_pydantic_params` | Converts Pydantic model parameters to flat kwargs |
| `resolve_target` | Resolves "module.path:function_name" to callable |
| `SCANNER_VERB_MAP` | Canonical mapping of scanner verbs (`get` / `list` / `create` / ...) to HTTP methods |
| `resolve_http_verb` | Resolves an HTTP method string to its scanner verb (with optional URL-path heuristic for `GET`) |
| `generate_suggested_alias` | Suggests a stable scanner alias for an endpoint based on path and verb |
| `has_path_params` | Returns `True` if a URL path template contains `{name}` or `:name` placeholders |
| `extract_path_param_names` | Extracts the ordered list of path-parameter names from a URL path template |
| `substitute_path_params` | Substitutes path-parameter values into a URL path template (raises on unknown params) |
| `enrich_schema_descriptions` | Merges descriptions into JSON Schema properties |
| `get_writer` | Factory function for writer instances |
| `DisplayResolver` | Sparse binding.yaml display overlay — resolves surface-facing alias, description, guidance, tags into `metadata["display"]` (§5.13) |
| `ConventionScanner` | Scans a `commands/` directory of plain Python files for public functions and converts them to `ScannedModule` instances with schema inferred from type annotations (§5.14) |
| `extract_input_schema` | Merges OpenAPI query, path, and request body params into a single JSON Schema |
| `extract_output_schema` | Extracts response schema from OpenAPI operation objects |
| `resolve_ref` | Resolves a single internal `$ref` JSON pointer |
| `resolve_schema` | Resolves a top-level `$ref` in a schema |
| `deep_resolve_refs` | Recursively resolves all `$ref` pointers, depth-limited to 16 levels |
| `annotations_to_dict` | Converts `ModuleAnnotations` to a plain dict |
| `module_to_dict` | Converts a `ScannedModule` to a dict for JSON/YAML serialization |
| `modules_to_dicts` | Batch version of `module_to_dict` |
| `run_verifier_chain` | Runs multiple verifiers in sequence, stopping on first failure |

## Usage

### Scanning and Writing

```python
from apcore_toolkit import BaseScanner, ScannedModule, YAMLWriter

class MyScanner(BaseScanner):
    def scan(self, **kwargs):
        # Scan your framework endpoints and return ScannedModule instances
        return [
            ScannedModule(
                module_id="users.get_user",
                description="Get a user by ID",
                input_schema={"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
                output_schema={"type": "object", "properties": {"name": {"type": "string"}}},
                tags=["users"],
                target="myapp.views:get_user",
            )
        ]

    def get_source_name(self):
        return "my-framework"

scanner = MyScanner()
modules = scanner.scan()

# Filter and deduplicate
modules = scanner.filter_modules(modules, include=r"^users\.")
modules = scanner.deduplicate_ids(modules)

# Write YAML binding files
writer = YAMLWriter()
writer.write(modules, output_dir="./bindings")
```

### Direct Registry Registration

```python
from apcore import Registry
from apcore_toolkit import RegistryWriter

registry = Registry()
writer = RegistryWriter()
writer.write(modules, registry)
```

### Conformance Verification

Approval and ACL gating key on a module's `requires_approval` annotation, which is
only reachable if annotations survive `scan → register → get_definition`. A writer
that drops them disables that gate **silently**. Run the shared conformance check in
your adapter's test suite so any such regression fails loudly:

```python
from apcore import ModuleAnnotations, Registry
from apcore_toolkit import assert_annotations_preserved

def test_writer_preserves_annotations():
    module = my_scanned_module(  # a ScannedModule with a resolvable target
        annotations=ModuleAnnotations(destructive=True, requires_approval=True),
    )
    # Raises AssertionError if annotations were dropped or changed.
    assert_annotations_preserved(MyRegistryWriter(), module, Registry())
```

### Output Format Factory

```python
from apcore_toolkit.output import get_writer

writer = get_writer("yaml")    # YAMLWriter
writer = get_writer("python")  # PythonWriter
writer = get_writer("registry")  # RegistryWriter
```

### Pydantic Model Flattening

```python
from apcore_toolkit import flatten_pydantic_params, resolve_target

# Resolve a target string to a callable
func = resolve_target("myapp.views:create_task")

# Flatten Pydantic model params into scalar kwargs for MCP tools
wrapped = flatten_pydantic_params(func)
```

### OpenAPI Schema Extraction

```python
from apcore_toolkit.openapi import extract_input_schema, extract_output_schema

input_schema = extract_input_schema(operation, openapi_doc)
output_schema = extract_output_schema(operation, openapi_doc)
```

### Schema Enrichment

```python
from apcore_toolkit import enrich_schema_descriptions

enriched = enrich_schema_descriptions(schema, {"user_id": "The user ID"})
```

### Markdown Formatting

```python
from apcore_toolkit import to_markdown

md = to_markdown({"name": "Alice", "role": "admin"}, title="User Info")
```

### Surface-Aware Formatters (v0.6.0)

`format_module` / `format_modules` / `format_schema` render `ScannedModule` and JSON Schema for specific consumer surfaces — LLM context, agent skill files, CLI listings, or programmatic JSON.

```python
from apcore_toolkit import format_module, format_modules, format_schema

# Module styles: "markdown" (default), "skill", "table-row", "json"
md = format_module(module, style="markdown")
skill_file = format_module(module, style="skill")        # ---\nname: ...\ndescription: ...\n---
row = format_module(module, style="table-row")           # CLI listing row
payload = format_module(module, style="json")            # dict (for APIs)

# Batch with optional grouping by tag or module-id prefix
listing = format_modules(modules, style="markdown", group_by="tag")

# Schema styles: "prose" (default), "table", "json"
prose = format_schema(schema, style="prose", max_depth=3)
table = format_schema(schema, style="table")
```

See `apcore-toolkit/docs/features/formatting.md` for the full contract.

### Tabular Formats (v0.7.0)

Byte-equivalent CSV / JSONL emitters with a cross-SDK conformance contract — Python, TypeScript, and Rust produce identical bytes for the same input.

```python
from apcore_toolkit import format_csv, format_jsonl

rows = [
    {"sn": 1, "title": "First", "score": 78},
    {"sn": 2, "title": "Second", "score": 82, "description": "later-only field"},
]

# CSV: header = union of keys across all rows (no silent data loss on
# heterogeneous rows); nested values serialized as canonical compact JSON;
# RFC 4180 CRLF line terminator.
csv_text = format_csv(rows)
# 'sn,title,score,description\r\n1,First,78,\r\n2,Second,82,later-only field\r\n'

# JSONL: canonical compact JSON per row, LF terminator, no trailing blank.
jsonl_text = format_jsonl(rows)

# UTF-8 BOM for Excel locales (default off for pipeline consumers):
csv_for_excel = format_csv(rows, bom=True)
```

See `apcore-toolkit/docs/features/formatting.md` § Tabular Formats for the full contract and `apcore-toolkit/conformance/fixtures/format_csv.json` / `format_jsonl.json` for the shared cross-SDK test corpus.

### Display Overlay (§5.13)

`DisplayResolver` applies a sparse `binding.yaml` display overlay to a list of `ScannedModule` instances, populating `metadata["display"]` with surface-facing presentation fields (alias, description, guidance, tags) for CLI, MCP, and A2A surfaces.

```python
from apcore_toolkit.display import DisplayResolver

resolver = DisplayResolver()

# Apply overlay from a directory of *.binding.yaml files
modules = resolver.resolve(scanned_modules, binding_path="bindings/")

# Or from a pre-parsed dict
modules = resolver.resolve(
    scanned_modules,
    binding_data={
        "bindings": [
            {
                "module_id": "product.get",
                "display": {
                    "alias": "product-get",
                    "description": "Get a product by ID",
                    "cli": {"alias": "get-product"},
                    "mcp": {"alias": "get_product"},
                },
            }
        ]
    },
)

# Resolved fields are in metadata["display"]
mod = modules[0]
print(mod.metadata["display"]["cli"]["alias"])   # "get-product"
print(mod.metadata["display"]["mcp"]["alias"])   # "get_product"
print(mod.metadata["display"]["a2a"]["alias"])   # "product-get"
```

**Resolution chain** (per field): surface-specific override > `display` default > binding-level field > scanner value.

**MCP alias constraints**: automatically sanitized (non-`[a-zA-Z0-9_-]` chars replaced with `_`; leading digit prefixed with `_`); raises `ValueError` if result exceeds 64 characters.

**CLI alias validation**: warns and falls back to `display.alias` when a user-explicitly-set alias does not match `^[a-z][a-z0-9_-]*$`.

### Convention Module Discovery (§5.14)

`ConventionScanner` scans a directory of plain Python files for public functions and converts them to `ScannedModule` instances. No decorators, no base classes, no imports from apcore -- just functions with type hints.

```python
from apcore_toolkit import ConventionScanner

scanner = ConventionScanner()
modules = scanner.scan(commands_dir="commands/")

# Each public function becomes a module:
#   commands/deploy.py  ->  deploy.deploy
#   commands/deploy.py  ->  deploy.rollback  (if rollback() exists)
```

Module-level constants customize behavior:

```python
# commands/deploy.py
MODULE_PREFIX = "ops"       # override file-based prefix -> ops.deploy
CLI_GROUP = "operations"    # group hint for CLI surface
TAGS = ["infra", "deploy"]  # tags stored in metadata

def deploy(env: str, tag: str = "latest") -> dict:
    """Deploy the app to the given environment."""
    return {"status": "deployed", "env": env}
```

Input and output schemas are inferred from PEP 484 type annotations. Use `include` / `exclude` regex filters to control which module IDs are registered.


## Documentation

Full documentation is available at [https://github.com/aiperceivable/apcore-toolkit](https://github.com/aiperceivable/apcore-toolkit).

## License

Apache-2.0
