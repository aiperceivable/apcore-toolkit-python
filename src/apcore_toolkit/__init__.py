"""apcore-toolkit — Shared scanner, schema extraction, and output toolkit.

Public API re-exports for convenient access to core types and utilities.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _get_version
from apcore_toolkit.ai_enhancer import AIEnhancer, Enhancer
from apcore_toolkit.display import DisplayResolver
from apcore_toolkit.formatting import to_markdown
from apcore_toolkit.openapi import (
    extract_input_schema,
    extract_output_schema,
    resolve_ref,
    resolve_schema,
)
from apcore_toolkit.output import get_writer
from apcore_toolkit.output.errors import WriteError
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
from apcore_toolkit.scanner import BaseScanner
from apcore_toolkit.schema_utils import enrich_schema_descriptions
from apcore_toolkit.serializers import annotations_to_dict, module_to_dict, modules_to_dicts
from apcore_toolkit.types import ScannedModule

try:
    __version__ = _get_version("apcore-toolkit")
except PackageNotFoundError:
    __version__ = "unknown"

__all__ = [
    "AIEnhancer",
    "DisplayResolver",
    "BaseScanner",
    "ConventionScanner",
    "HTTPProxyRegistryWriter",
    "Enhancer",
    "JSONVerifier",
    "MagicBytesVerifier",
    "PythonWriter",
    "RegistryVerifier",
    "RegistryWriter",
    "ScannedModule",
    "SyntaxVerifier",
    "Verifier",
    "VerifyResult",
    "WriteError",
    "WriteResult",
    "YAMLVerifier",
    "YAMLWriter",
    "annotations_to_dict",
    "enrich_schema_descriptions",
    "extract_input_schema",
    "extract_output_schema",
    "flatten_pydantic_params",
    "get_writer",
    "module_to_dict",
    "modules_to_dicts",
    "resolve_ref",
    "resolve_schema",
    "resolve_target",
    "run_verifier_chain",
    "to_markdown",
]
