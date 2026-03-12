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
