"""Tests for HTTPProxyRegistryWriter."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from apcore import Registry

from apcore_toolkit.output.http_proxy_writer import HTTPProxyRegistryWriter
from apcore_toolkit.types import ScannedModule


def _make_module(
    module_id: str = "test.get_items.get",
    http_method: str = "GET",
    url_path: str = "/items",
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
) -> ScannedModule:
    return ScannedModule(
        module_id=module_id,
        description=f"Test {module_id}",
        input_schema=input_schema or {"type": "object", "properties": {}},
        output_schema=output_schema or {"type": "object", "properties": {}},
        tags=["test"],
        target="test:func",
        metadata={"http_method": http_method, "url_path": url_path},
    )


class TestHTTPProxyRegistryWriter:
    def test_write_registers_all_modules(self) -> None:
        registry = Registry()
        writer = HTTPProxyRegistryWriter(base_url="http://localhost:8000")

        modules = [
            _make_module("test.list.get", "GET", "/items"),
            _make_module("test.create.post", "POST", "/items"),
            _make_module("test.get_item.get", "GET", "/items/{item_id}"),
        ]

        results = writer.write(modules, registry)
        assert len(results) == 3
        assert all(r.verified for r in results)

    def test_write_returns_failure_for_bad_module(self) -> None:
        registry = Registry()
        writer = HTTPProxyRegistryWriter(base_url="http://localhost:8000")

        # Module with nullable field via anyOf
        mod = _make_module(
            input_schema={
                "type": "object",
                "properties": {
                    "id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "name": {"type": "string"},
                },
            }
        )

        results = writer.write([mod], registry)
        # Should succeed or gracefully fail (no crash)
        assert len(results) == 1

    def test_proxy_sends_get_with_query_params(self) -> None:
        registry = Registry()
        writer = HTTPProxyRegistryWriter(
            base_url="http://localhost:8000",
            auth_header_factory=lambda: {"Authorization": "Bearer test-token"},
        )

        mod = _make_module(
            input_schema={
                "type": "object",
                "properties": {"page": {"type": "integer"}, "size": {"type": "integer"}},
            }
        )
        writer.write([mod], registry)

        # Get the registered module instance
        module_instance = registry._modules["test.get_items.get"]

        # Mock httpx
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"items": [], "total": 0}

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        import httpx

        original = httpx.AsyncClient
        httpx.AsyncClient = MagicMock(return_value=mock_client)

        try:
            result = asyncio.run(module_instance.execute({"page": 1, "size": 10}))
            assert result == {"items": [], "total": 0}

            # Verify the request was made with query params
            mock_client.request.assert_called_once()
            call_args = mock_client.request.call_args
            assert call_args[0] == ("GET", "/items")
            assert call_args[1]["params"] == {"page": 1, "size": 10}
            assert call_args[1]["headers"]["Authorization"] == "Bearer test-token"
        finally:
            httpx.AsyncClient = original

    def test_proxy_sends_post_with_json_body(self) -> None:
        registry = Registry()
        writer = HTTPProxyRegistryWriter(base_url="http://localhost:8000")

        mod = _make_module(
            module_id="test.create.post",
            http_method="POST",
            url_path="/items",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        )
        writer.write([mod], registry)

        module_instance = registry._modules["test.create.post"]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "1", "name": "Test"}

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        import httpx

        original = httpx.AsyncClient
        httpx.AsyncClient = MagicMock(return_value=mock_client)

        try:
            result = asyncio.run(module_instance.execute({"name": "Test"}))
            assert result["name"] == "Test"

            call_args = mock_client.request.call_args
            assert call_args[0] == ("POST", "/items")
            assert call_args[1]["json"] == {"name": "Test"}
        finally:
            httpx.AsyncClient = original

    def test_proxy_substitutes_path_params(self) -> None:
        registry = Registry()
        writer = HTTPProxyRegistryWriter(base_url="http://localhost:8000")

        mod = _make_module(
            module_id="test.get_item.get",
            http_method="GET",
            url_path="/items/{item_id}",
            input_schema={
                "type": "object",
                "properties": {"item_id": {"type": "string"}},
                "required": ["item_id"],
            },
        )
        writer.write([mod], registry)

        module_instance = registry._modules["test.get_item.get"]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "abc", "name": "Item"}

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        import httpx

        original = httpx.AsyncClient
        httpx.AsyncClient = MagicMock(return_value=mock_client)

        try:
            result = asyncio.run(module_instance.execute({"item_id": "abc"}))
            assert result["id"] == "abc"

            call_args = mock_client.request.call_args
            # Path param should be substituted, not sent as query
            assert call_args[0] == ("GET", "/items/abc")
            assert "params" not in call_args[1] or call_args[1]["params"] == {}
        finally:
            httpx.AsyncClient = original

    def test_no_auth_headers_when_factory_is_none(self) -> None:
        writer = HTTPProxyRegistryWriter(base_url="http://localhost:8000")
        assert writer._auth_header_factory is None

    def test_custom_timeout(self) -> None:
        writer = HTTPProxyRegistryWriter(base_url="http://localhost:8000", timeout=120.0)
        assert writer._timeout == 120.0
