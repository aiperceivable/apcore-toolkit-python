"""apcore-toolkit — Shared scanner, schema extraction, and output toolkit.

Public API re-exports for convenient access to core types and utilities.
"""

from apcore_toolkit.formatting import to_markdown
from apcore_toolkit.output.python_writer import PythonWriter
from apcore_toolkit.output.registry_writer import RegistryWriter
from apcore_toolkit.output.yaml_writer import YAMLWriter
from apcore_toolkit.pydantic_utils import flatten_pydantic_params, resolve_target
from apcore_toolkit.scanner import BaseScanner
from apcore_toolkit.schema_utils import enrich_schema_descriptions
from apcore_toolkit.types import ScannedModule

__version__ = "0.1.0"

__all__ = [
    "BaseScanner",
    "PythonWriter",
    "RegistryWriter",
    "ScannedModule",
    "YAMLWriter",
    "enrich_schema_descriptions",
    "flatten_pydantic_params",
    "resolve_target",
    "to_markdown",
]
