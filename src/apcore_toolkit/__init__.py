"""apcore-toolkit — Shared scanner, schema extraction, and output toolkit.

Public API re-exports for convenient access to core types and utilities.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _get_version
from apcore_toolkit.ai_enhancer import AIEnhancer, Enhancer
from apcore_toolkit.binding_loader import BindingLoader, BindingLoadError
from apcore_toolkit.display import DisplayResolver
from apcore_toolkit.formatting import (
    format_module,
    format_modules,
    format_schema,
    to_markdown,
)
from apcore_toolkit.openapi import (
    deep_resolve_refs,
    extract_input_schema,
    extract_output_schema,
    resolve_ref,
    resolve_schema,
)
from apcore_toolkit.output import get_writer
from apcore_toolkit.output.errors import InvalidFormatError, WriteError
from apcore_toolkit.output.python_writer import PythonWriter
from apcore_toolkit.output.http_proxy_writer import HTTPProxyRegistryWriter
from apcore_toolkit.output.registry_writer import RegistryWriter
from apcore_toolkit.output.types import Verifier, VerifyResult, WriteResult
from apcore_toolkit.output.verifiers import (
    JSONVerifier,
    MagicBytesVerifier,
    RegistryVerifier,
    SyntaxVerifier,
    YAMLVerifier,
    run_verifier_chain,
)
from apcore_toolkit.output.yaml_writer import YAMLWriter
from apcore_toolkit.pydantic_utils import flatten_pydantic_params, resolve_target
from apcore_toolkit.convention_scanner import ConventionScanner
from apcore_toolkit.http_verb_map import (
    SCANNER_VERB_MAP,
    extract_path_param_names,
    generate_suggested_alias,
    has_path_params,
    resolve_http_verb,
    substitute_path_params,
)
from apcore_toolkit.scanner import (
    BaseScanner,
    deduplicate_ids,
    filter_modules,
    infer_annotations_from_method,
)
from apcore_toolkit.schema_utils import enrich_schema_descriptions
from apcore_toolkit.serializers import annotations_to_dict, module_to_dict, modules_to_dicts
from apcore_toolkit.types import ScannedModule, clone_module, create_scanned_module

try:
    __version__ = _get_version("apcore-toolkit")
except PackageNotFoundError:
    __version__ = "unknown"

__all__ = [
    "AIEnhancer",
    "BindingLoadError",
    "BindingLoader",
    "DisplayResolver",
    "BaseScanner",
    "ConventionScanner",
    "HTTPProxyRegistryWriter",
    "Enhancer",
    "InvalidFormatError",
    "JSONVerifier",
    "MagicBytesVerifier",
    "PythonWriter",
    "RegistryVerifier",
    "RegistryWriter",
    "SCANNER_VERB_MAP",
    "ScannedModule",
    "SyntaxVerifier",
    "Verifier",
    "VerifyResult",
    "WriteError",
    "WriteResult",
    "YAMLVerifier",
    "YAMLWriter",
    "annotations_to_dict",
    "clone_module",
    "create_scanned_module",
    "deduplicate_ids",
    "deep_resolve_refs",
    "enrich_schema_descriptions",
    "extract_input_schema",
    "extract_output_schema",
    "extract_path_param_names",
    "filter_modules",
    "flatten_pydantic_params",
    "format_module",
    "format_modules",
    "format_schema",
    "generate_suggested_alias",
    "get_writer",
    "has_path_params",
    "infer_annotations_from_method",
    "module_to_dict",
    "modules_to_dicts",
    "resolve_http_verb",
    "resolve_ref",
    "resolve_schema",
    "resolve_target",
    "run_verifier_chain",
    "substitute_path_params",
    "to_markdown",
]
