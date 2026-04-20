"""Tests for apcore_toolkit.output — get_writer factory."""

from __future__ import annotations

import pytest

from apcore_toolkit.output import get_writer
from apcore_toolkit.output.http_proxy_writer import HTTPProxyRegistryWriter
from apcore_toolkit.output.python_writer import PythonWriter
from apcore_toolkit.output.registry_writer import RegistryWriter
from apcore_toolkit.output.yaml_writer import YAMLWriter


class TestGetWriter:
    def test_yaml(self) -> None:
        writer = get_writer("yaml")
        assert isinstance(writer, YAMLWriter)

    def test_python(self) -> None:
        writer = get_writer("python")
        assert isinstance(writer, PythonWriter)

    def test_registry(self) -> None:
        writer = get_writer("registry")
        assert isinstance(writer, RegistryWriter)

    def test_http_proxy(self) -> None:
        writer = get_writer("http-proxy", base_url="http://localhost:8000")
        assert isinstance(writer, HTTPProxyRegistryWriter)

    def test_unknown_format(self) -> None:
        with pytest.raises(ValueError, match="Unknown output format"):
            get_writer("json")

    def test_unknown_format_message(self) -> None:
        with pytest.raises(ValueError, match="'json'"):
            get_writer("json")

    def test_yaml_rejects_unexpected_kwargs(self) -> None:
        with pytest.raises(TypeError, match="YAMLWriter"):
            get_writer("yaml", base_url="http://localhost")

    def test_python_rejects_unexpected_kwargs(self) -> None:
        with pytest.raises(TypeError, match="PythonWriter"):
            get_writer("python", timeout=5)

    def test_registry_rejects_unexpected_kwargs(self) -> None:
        with pytest.raises(TypeError, match="RegistryWriter"):
            get_writer("registry", extra="ignored")
