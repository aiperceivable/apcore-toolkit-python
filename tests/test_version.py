from importlib.metadata import PackageNotFoundError
from runpy import run_path
from unittest.mock import patch

import pytest

import apcore_toolkit


@pytest.mark.parametrize(("installed_version", "expected"), [("1.2.3", "1.2.3"), (None, "unknown")])
def test_version_reads_toolkit_distribution_metadata(installed_version: str | None, expected: str) -> None:
    assert apcore_toolkit.__file__ is not None
    missing_metadata = PackageNotFoundError("apcore-toolkit") if installed_version is None else None
    with patch("importlib.metadata.version", return_value=installed_version, side_effect=missing_metadata) as version:
        assert run_path(apcore_toolkit.__file__)["__version__"] == expected

    version.assert_called_once_with("apcore-toolkit")
