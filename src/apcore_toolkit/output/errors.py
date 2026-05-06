"""Error types for output writers."""

from __future__ import annotations


class WriteError(Exception):
    """Raised when a writer fails to write an artifact to disk.

    Attributes:
        path: The file path that could not be written.
        cause: The underlying exception that caused the failure.
    """

    def __init__(self, path: str, cause: Exception) -> None:
        self.path = path
        self.cause = cause
        super().__init__(f"Failed to write {path}: {cause}")


class InvalidFormatError(ValueError):
    """Raised by ``get_writer`` when the requested output format is unknown.

    Subclasses ``ValueError`` so existing callers catching ``ValueError``
    keep working while cross-language consumers can catch the typed error
    directly. Mirrors TypeScript's ``InvalidFormatError`` and Rust's
    ``OutputFormatError::Unknown(format)`` so the three SDKs expose a
    consistent error contract.

    Attributes:
        format: The unknown format name that was passed to ``get_writer``.
    """

    def __init__(self, output_format: str) -> None:
        self.format = output_format
        super().__init__(f"Unknown output format: {output_format!r}")
