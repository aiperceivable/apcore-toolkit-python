"""Output writers for ScannedModule data.

Provides a factory function to obtain a writer by format name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apcore_toolkit.output.http_proxy_writer import HTTPProxyRegistryWriter

from apcore_toolkit.output.errors import InvalidFormatError as InvalidFormatError
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


def get_writer(
    output_format: str, **kwargs: Any
) -> YAMLWriter | PythonWriter | RegistryWriter | HTTPProxyRegistryWriter:
    """Return a writer instance for the given output format.

    Args:
        output_format: Output format name (``"yaml"``, ``"python"``,
            ``"registry"``, or ``"http-proxy"``).
        **kwargs: Passed to the writer constructor. For ``"http-proxy"``:
            ``base_url``, ``auth_header_factory``, ``timeout``.

    Returns:
        A writer instance.

    Raises:
        InvalidFormatError: If the format is not recognized. Subclass of
            ``ValueError`` so existing ``except ValueError`` callers keep
            working; mirrors TypeScript ``InvalidFormatError`` and Rust
            ``OutputFormatError::Unknown`` for cross-SDK parity.
    """
    if output_format == "yaml":
        if kwargs:
            raise TypeError(f"YAMLWriter accepts no keyword arguments, got: {sorted(kwargs)}")
        return YAMLWriter()
    if output_format == "python":
        if kwargs:
            raise TypeError(f"PythonWriter accepts no keyword arguments, got: {sorted(kwargs)}")
        return PythonWriter()
    if output_format == "registry":
        if kwargs:
            raise TypeError(f"RegistryWriter accepts no keyword arguments, got: {sorted(kwargs)}")
        return RegistryWriter()
    if output_format == "http-proxy":
        from apcore_toolkit.output.http_proxy_writer import HTTPProxyRegistryWriter

        return HTTPProxyRegistryWriter(**kwargs)

    raise InvalidFormatError(output_format)
