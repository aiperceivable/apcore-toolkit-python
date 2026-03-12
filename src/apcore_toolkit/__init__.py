"""apcore-toolkit — Shared scanner, schema extraction, and output toolkit.

Public API re-exports for convenient access to core types and utilities.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _get_version
from apcore_toolkit.ai_enhancer import AIEnhancer
from apcore_toolkit.formatting import to_markdown
from apcore_toolkit.output import get_writer
from apcore_toolkit.output.python_writer import PythonWriter
from apcore_toolkit.output.registry_writer import RegistryWriter
from apcore_toolkit.output.types import WriteResult
from apcore_toolkit.output.yaml_writer import YAMLWriter
from apcore_toolkit.pydantic_utils import flatten_pydantic_params, resolve_target
from apcore_toolkit.scanner import BaseScanner
from apcore_toolkit.schema_utils import enrich_schema_descriptions
from apcore_toolkit.types import ScannedModule

try:
    __version__ = _get_version("apcore-toolkit")
except PackageNotFoundError:
    __version__ = "unknown"

__all__ = [
    "AIEnhancer",
    "BaseScanner",
    "PythonWriter",
    "RegistryWriter",
    "ScannedModule",
    "WriteResult",
    "YAMLWriter",
    "enrich_schema_descriptions",
    "flatten_pydantic_params",
    "get_writer",
    "resolve_target",
    "to_markdown",
]
