"""Output writers for ScannedModule data.

Provides a factory function to obtain a writer by format name.
"""

from __future__ import annotations

from apcore_toolkit.output.errors import WriteError as WriteError
from apcore_toolkit.output.python_writer import PythonWriter
from apcore_toolkit.output.registry_writer import RegistryWriter
from apcore_toolkit.output.types import Verifier as Verifier
from apcore_toolkit.output.types import VerifyResult as VerifyResult
from apcore_toolkit.output.types import WriteResult as WriteResult
from apcore_toolkit.output.verifiers import JSONVerifier as JSONVerifier
from apcore_toolkit.output.verifiers import MagicBytesVerifier as MagicBytesVerifier
from apcore_toolkit.output.verifiers import RegistryVerifier as RegistryVerifier
from apcore_toolkit.output.verifiers import SyntaxVerifier as SyntaxVerifier
from apcore_toolkit.output.verifiers import YAMLVerifier as YAMLVerifier
from apcore_toolkit.output.yaml_writer import YAMLWriter


def get_writer(output_format: str) -> YAMLWriter | PythonWriter | RegistryWriter:
    """Return a writer instance for the given output format.

    Args:
        output_format: Output format name (``"yaml"``, ``"python"``, or ``"registry"``).

    Returns:
        A writer instance.

    Raises:
        ValueError: If the format is not recognized.
    """
    if output_format == "yaml":
        return YAMLWriter()
    if output_format == "python":
        return PythonWriter()
    if output_format == "registry":
        return RegistryWriter()
    raise ValueError(f"Unknown output format: {output_format!r}")
