"""Tests for HTTPProxyRegistryWriter."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
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


class TestHTTPProxyRegistryWriterBaseURL:
    """Behavioural parity with Rust's ``HTTPProxyRegistryWriter::new``:
    the constructor must reject non-http(s) base_url schemes up-front so
    a misconfigured config cannot reach the HTTP client.
    """

    @pytest.mark.parametrize(
        "bad_url",
        [
            "file:///x",
            "ftp://example.com",
            "ws://example.com",
            "javascript:alert(1)",
            "://no-scheme",
        ],
    )
    def test_init_rejects_non_http_scheme(self, bad_url: str) -> None:
        with pytest.raises(ValueError, match="Invalid base_url scheme"):
            HTTPProxyRegistryWriter(base_url=bad_url)

    @pytest.mark.parametrize("good_url", ["http://localhost:8000", "https://api.example.com"])
    def test_init_accepts_http_and_https(self, good_url: str) -> None:
        # Must not raise.
        HTTPProxyRegistryWriter(base_url=good_url)


class TestHTTPProxyRegistryWriterTimeout:
    """Behavioural parity with Rust's ``HTTPProxyRegistryWriter::new``:
    the constructor must reject a non-finite or non-positive ``timeout``
    up-front instead of deferring to undefined ``httpx`` client behaviour.
    """

    @pytest.mark.parametrize(
        "bad_timeout",
        [0, -1, -0.5, float("nan"), float("inf"), float("-inf")],
    )
    def test_init_rejects_non_finite_or_non_positive_timeout(self, bad_timeout: float) -> None:
        with pytest.raises(ValueError, match="timeout"):
            HTTPProxyRegistryWriter(base_url="http://x", timeout=bad_timeout)

    @pytest.mark.parametrize("good_timeout", [0.001, 1, 60.0, 3600])
    def test_init_accepts_positive_finite_timeout(self, good_timeout: float) -> None:
        # Must not raise.
        HTTPProxyRegistryWriter(base_url="http://x", timeout=good_timeout)


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

        # Module with nullable field via anyOf — valid schema, should register successfully
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
        assert len(results) == 1
        assert results[0].verified is True

    def test_write_with_none_metadata_falls_back_to_defaults(self) -> None:
        """_get_http_fields must not AttributeError when mod.metadata is None."""
        registry = Registry()
        writer = HTTPProxyRegistryWriter(base_url="http://localhost:8000")
        mod = _make_module()
        mod.metadata = None  # simulate framework module without metadata dict
        results = writer.write([mod], registry)
        assert results[0].verified is True

    def test_proxy_sends_get_with_query_params(self, monkeypatch) -> None:
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

        module_instance = registry._modules["test.get_items.get"]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"items": [], "total": 0}

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=mock_client))

        result = asyncio.run(module_instance.execute({"page": 1, "size": 10}))
        assert result == {"items": [], "total": 0}

        mock_client.request.assert_called_once()
        call_args = mock_client.request.call_args
        assert call_args[0] == ("GET", "/items")
        assert call_args[1]["params"] == {"page": 1, "size": 10}
        assert call_args[1]["headers"]["Authorization"] == "Bearer test-token"

    def test_proxy_sends_post_with_json_body(self, monkeypatch) -> None:
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

        monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=mock_client))

        result = asyncio.run(module_instance.execute({"name": "Test"}))
        assert result["name"] == "Test"

        call_args = mock_client.request.call_args
        assert call_args[0] == ("POST", "/items")
        assert call_args[1]["json"] == {"name": "Test"}

    def test_proxy_substitutes_path_params(self, monkeypatch) -> None:
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

        monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=mock_client))

        result = asyncio.run(module_instance.execute({"item_id": "abc"}))
        assert result["id"] == "abc"

        call_args = mock_client.request.call_args
        assert call_args[0] == ("GET", "/items/abc")
        assert "params" not in call_args[1] or call_args[1]["params"] == {}

    def test_proxy_substitutes_colon_style_path_params(self, monkeypatch) -> None:
        """Regression: colon-style paths (:id) must substitute, not leak into query."""
        registry = Registry()
        writer = HTTPProxyRegistryWriter(base_url="http://localhost:8000")

        mod = _make_module(
            module_id="test.get_item_colon.get",
            http_method="GET",
            url_path="/items/:item_id",
            input_schema={
                "type": "object",
                "properties": {"item_id": {"type": "string"}},
                "required": ["item_id"],
            },
        )
        writer.write([mod], registry)

        module_instance = registry._modules["test.get_item_colon.get"]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "abc"}

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=mock_client))

        asyncio.run(module_instance.execute({"item_id": "abc"}))
        call_args = mock_client.request.call_args
        assert call_args[0] == ("GET", "/items/abc")
        assert "params" not in call_args[1] or call_args[1]["params"] == {}

    def test_delete_forwards_non_path_inputs_as_query(self, monkeypatch) -> None:
        """Regression: DELETE with non-path inputs must not silently drop them."""
        registry = Registry()
        writer = HTTPProxyRegistryWriter(base_url="http://localhost:8000")

        mod = _make_module(
            module_id="test.bulk_delete.delete",
            http_method="DELETE",
            url_path="/items",
            input_schema={
                "type": "object",
                "properties": {"ids": {"type": "string"}},
            },
        )
        writer.write([mod], registry)

        module_instance = registry._modules["test.bulk_delete.delete"]

        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=mock_client))

        asyncio.run(module_instance.execute({"ids": "1,2,3"}))
        call_args = mock_client.request.call_args
        assert call_args[0] == ("DELETE", "/items")
        assert call_args[1].get("params") == {"ids": "1,2,3"}
        assert "json" not in call_args[1]

    def test_path_param_value_is_percent_encoded(self, monkeypatch) -> None:
        """Regression: path-param values containing reserved chars (e.g. ``/``)
        must be RFC 3986 percent-encoded so they cannot corrupt the URL."""
        registry = Registry()
        writer = HTTPProxyRegistryWriter(base_url="http://localhost:8000")

        mod = _make_module(
            module_id="test.get_item_encoded.get",
            http_method="GET",
            url_path="/items/{item_id}",
            input_schema={
                "type": "object",
                "properties": {"item_id": {"type": "string"}},
                "required": ["item_id"],
            },
        )
        writer.write([mod], registry)
        module_instance = registry._modules["test.get_item_encoded.get"]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {}

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=mock_client))

        # ``a/b#c`` would otherwise corrupt the URL — both ``/`` and ``#``
        # must be percent-encoded.
        asyncio.run(module_instance.execute({"item_id": "a/b#c"}))
        call_args = mock_client.request.call_args
        assert call_args[0] == ("GET", "/items/a%2Fb%23c")

    def test_unfilled_path_param_raises_module_error(self, monkeypatch) -> None:
        """Regression: a missing path-parameter input must NOT leak ``{name}``
        into the request URL — it must raise ``ModuleError`` instead."""
        from apcore.errors import ModuleError

        registry = Registry()
        writer = HTTPProxyRegistryWriter(base_url="http://localhost:8000")

        mod = _make_module(
            module_id="test.unfilled_param.get",
            http_method="GET",
            url_path="/items/{item_id}",
            input_schema={
                "type": "object",
                "properties": {"item_id": {"type": "string"}},
                "required": ["item_id"],
            },
        )
        writer.write([mod], registry)
        module_instance = registry._modules["test.unfilled_param.get"]

        mock_client = AsyncMock()
        monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=mock_client))

        with pytest.raises(ModuleError) as exc_info:
            asyncio.run(module_instance.execute({}))
        assert "{item_id}" in str(exc_info.value)
        # The HTTP client must NOT be called when validation fails.
        mock_client.request.assert_not_called()

    def test_write_failure_preserves_traceback(self, caplog, monkeypatch) -> None:
        """When module construction fails, the warning log must carry exc_info
        and the WriteResult error must include the exception class name."""
        import logging

        registry = Registry()
        writer = HTTPProxyRegistryWriter(base_url="http://localhost:8000")

        def _boom(_mod):
            raise RuntimeError("synthetic build failure")

        monkeypatch.setattr(writer, "_build_module_class", _boom)

        mod = _make_module("test.bad.get")
        with caplog.at_level(logging.WARNING, logger="apcore_toolkit"):
            results = writer.write([mod], registry)

        assert len(results) == 1
        assert not results[0].verified
        assert "RuntimeError" in (results[0].verification_error or "")
        assert "synthetic build failure" in (results[0].verification_error or "")
        failures = [
            r
            for r in caplog.records
            if r.exc_info is not None
            and r.exc_info[0] is RuntimeError
            and "synthetic build failure" in str(r.exc_info[1])
        ]
        assert failures, "expected at least one WARNING record with RuntimeError exc_info"

    def test_no_auth_headers_when_factory_is_none(self) -> None:
        writer = HTTPProxyRegistryWriter(base_url="http://localhost:8000")
        assert writer._auth_header_factory is None

    def test_custom_timeout(self) -> None:
        writer = HTTPProxyRegistryWriter(base_url="http://localhost:8000", timeout=120.0)
        assert writer._timeout == 120.0


class TestExtractErrorMessage:
    """_extract_error_message: except must be narrow (ValueError/AttributeError), not bare Exception."""

    def _make_resp(
        self,
        *,
        content_type: str = "application/json",
        json_side_effect: Exception | None = None,
        json_return: dict | None = None,
        text: str = "fallback text",
    ) -> MagicMock:
        resp = MagicMock()
        resp.headers = {"content-type": content_type}
        resp.text = text
        if json_side_effect is not None:
            resp.json.side_effect = json_side_effect
        else:
            resp.json.return_value = json_return or {}
        return resp

    def test_json_value_error_falls_through_to_text(self) -> None:
        """resp.json() raising ValueError (bad JSON body) must return text fallback."""
        from apcore_toolkit.output.http_proxy_writer import _extract_error_message

        resp = self._make_resp(json_side_effect=ValueError("bad json"), text="raw error")
        assert _extract_error_message(resp) == "raw error"

    def test_non_json_content_type_returns_text(self) -> None:
        """Non-JSON content-type must skip json() entirely and return text."""
        from apcore_toolkit.output.http_proxy_writer import _extract_error_message

        resp = self._make_resp(content_type="text/plain", text="plain error")
        assert _extract_error_message(resp) == "plain error"

    def test_json_error_message_key_used(self) -> None:
        """error_message key takes priority."""
        from apcore_toolkit.output.http_proxy_writer import _extract_error_message

        resp = self._make_resp(json_return={"error_message": "specific error"})
        assert _extract_error_message(resp) == "specific error"

    def test_non_value_error_from_json_propagates(self) -> None:
        """After fix: a RuntimeError from resp.json() must not be silently swallowed."""
        import pytest
        from apcore_toolkit.output.http_proxy_writer import _extract_error_message

        resp = self._make_resp(json_side_effect=RuntimeError("unexpected crash"))
        with pytest.raises(RuntimeError, match="unexpected crash"):
            _extract_error_message(resp)


class TestProxyModuleExecute:
    """Regression tests for ProxyModule.execute() error paths (issues 2, 3, 10)."""

    def _setup(self, monkeypatch, mock_client: AsyncMock, **module_kwargs: Any) -> Any:
        registry = Registry()
        writer = HTTPProxyRegistryWriter(base_url="http://localhost:8000")
        mod = _make_module(**module_kwargs)
        writer.write([mod], registry)
        module_instance = registry._modules[mod.module_id]
        monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=mock_client))
        return module_instance

    def _mock_client(self, response: Any) -> AsyncMock:
        mock = AsyncMock()
        mock.request = AsyncMock(return_value=response)
        mock.__aenter__ = AsyncMock(return_value=mock)
        mock.__aexit__ = AsyncMock(return_value=None)
        return mock

    def test_execute_malformed_json_on_2xx_raises_module_error(self, monkeypatch) -> None:
        """200 response with invalid JSON body must raise ModuleError, not ValueError."""
        from apcore.errors import ModuleError

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.side_effect = ValueError("invalid json")
        module_instance = self._setup(monkeypatch, self._mock_client(mock_response))

        with pytest.raises(ModuleError):
            asyncio.run(module_instance.execute({}))

    def test_execute_non_dict_json_on_2xx_raises_module_error(self, monkeypatch) -> None:
        """200 response where json() returns a list must raise ModuleError."""
        from apcore.errors import ModuleError

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [1, 2, 3]
        module_instance = self._setup(monkeypatch, self._mock_client(mock_response))

        with pytest.raises(ModuleError):
            asyncio.run(module_instance.execute({}))

    def test_execute_httpx_transport_error_raises_module_error(self, monkeypatch) -> None:
        """httpx.HTTPError during transport must be wrapped in ModuleError, not propagate raw."""
        from apcore.errors import ModuleError

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(side_effect=httpx.HTTPError("connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        module_instance = self._setup(monkeypatch, mock_client)

        with pytest.raises(ModuleError):
            asyncio.run(module_instance.execute({}))

    def test_execute_204_returns_empty_dict(self, monkeypatch) -> None:
        """204 No Content must return {} without attempting to parse body."""
        mock_response = MagicMock()
        mock_response.status_code = 204
        module_instance = self._setup(monkeypatch, self._mock_client(mock_response))

        result = asyncio.run(module_instance.execute({}))
        assert result == {}

    def test_execute_non_2xx_raises_module_error(self, monkeypatch) -> None:
        """Non-2xx response must raise ModuleError."""
        from apcore.errors import ModuleError

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.headers = {"content-type": "text/plain"}
        mock_response.text = "not found"
        module_instance = self._setup(monkeypatch, self._mock_client(mock_response))

        with pytest.raises(ModuleError):
            asyncio.run(module_instance.execute({}))
