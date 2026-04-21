"""Tests for WriteError exception."""

from __future__ import annotations

from apcore_toolkit.output.errors import WriteError


class TestWriteError:
    def test_attributes(self):
        cause = OSError("disk full")
        err = WriteError("/tmp/test.yaml", cause)
        assert err.path == "/tmp/test.yaml"
        assert err.cause is cause

    def test_message(self):
        cause = PermissionError("access denied")
        err = WriteError("/tmp/test.yaml", cause)
        assert "Failed to write /tmp/test.yaml" in str(err)
        assert "access denied" in str(err)

    def test_is_exception(self):
        err = WriteError("/tmp/x", OSError("fail"))
        assert isinstance(err, Exception)

    def test_importable_from_output_package(self):
        from apcore_toolkit.output import WriteError as WE

        assert WE is WriteError


class TestWriteErrorFromWriters:
    def test_yaml_writer_raises_write_error_on_io_failure(self, sample_module, tmp_path):
        """YAMLWriter surfaces a WriteError when the atomic rename fails.

        YAMLWriter uses an atomic tmp-file + ``os.replace`` pattern (ported
        from the TypeScript/Rust writers). Failures during the rename step
        must still surface as WriteError with the original cause embedded.
        """
        from unittest.mock import patch

        import pytest

        from apcore_toolkit import YAMLWriter
        from apcore_toolkit.output import yaml_writer as _yw

        writer = YAMLWriter()
        with patch.object(_yw.os, "replace", side_effect=PermissionError("access denied")):
            with pytest.raises(WriteError) as exc_info:
                writer.write([sample_module], output_dir=str(tmp_path))
        assert "access denied" in str(exc_info.value)

    def test_python_writer_raises_write_error_on_io_failure(self, sample_module):
        from unittest.mock import patch

        from apcore_toolkit import PythonWriter

        writer = PythonWriter()
        with patch("pathlib.Path.write_text", side_effect=PermissionError("no write")):
            with patch("pathlib.Path.mkdir"):
                with patch("pathlib.Path.exists", return_value=False):
                    import pytest

                    with pytest.raises(WriteError) as exc_info:
                        writer.write([sample_module], output_dir="/tmp/test_py_writer_err")
                    assert "no write" in str(exc_info.value)
