"""Tests for apcore_toolkit.formatting.tabular — format_csv / format_jsonl.

Covers the bug scenarios driving the toolkit-lift decision (see
``apcore-toolkit/docs/features/formatting.md`` § Tabular Formats):

- Heterogeneous row keys (the apcore-cli-typescript data-loss bug)
- Nested object / array cell values (the apcore-cli-python repr bug)
- RFC 4180 escaping (comma, quote, newline, CR)
- CRLF line terminator
- Cross-SDK number canonicalization (whole-number floats render as ints)
"""

from __future__ import annotations

import json

from apcore_toolkit.formatting.tabular import format_csv, format_jsonl


class TestFormatCsvBasic:
    def test_empty_input_returns_empty_string(self) -> None:
        assert format_csv([]) == ""

    def test_single_row(self) -> None:
        result = format_csv([{"a": 1, "b": 2}])
        assert result == "a,b\r\n1,2\r\n"

    def test_multiple_homogeneous_rows(self) -> None:
        result = format_csv([{"a": 1, "b": 2}, {"a": 3, "b": 4}])
        assert result == "a,b\r\n1,2\r\n3,4\r\n"

    def test_uses_crlf_line_terminator(self) -> None:
        result = format_csv([{"x": "v"}])
        assert result.endswith("\r\n")
        assert "\r\n" in result


class TestFormatCsvHeterogeneousKeys:
    """Regression for apcore-cli-typescript bug at src/output.ts:347-354 —
    keys derived from first row only, dropping later-row fields silently."""

    def test_later_row_extra_key_is_included(self) -> None:
        rows = [{"a": 1}, {"a": 2, "b": 3}]
        result = format_csv(rows)
        assert result == "a,b\r\n1,\r\n2,3\r\n"

    def test_first_row_extra_key_carries_empty_in_later_rows(self) -> None:
        rows = [{"a": 1, "b": 2}, {"a": 3}]
        result = format_csv(rows)
        assert result == "a,b\r\n1,2\r\n3,\r\n"

    def test_disjoint_keys_across_rows(self) -> None:
        rows = [{"a": 1}, {"b": 2}, {"c": 3}]
        result = format_csv(rows)
        assert result == "a,b,c\r\n1,,\r\n,2,\r\n,,3\r\n"

    def test_key_order_preserves_first_occurrence(self) -> None:
        rows = [{"x": 1, "y": 2}, {"z": 3, "x": 4}]
        result = format_csv(rows)
        assert result == "x,y,z\r\n1,2,\r\n4,,3\r\n"


class TestFormatCsvNestedValues:
    """Regression for apcore-cli-python bug at src/apcore_cli/output.py:149 —
    str() on nested dicts produces Python repr (single quotes), not JSON."""

    def test_nested_dict_serializes_as_canonical_json(self) -> None:
        row = {"schema": {"type": "object", "properties": {"a": {"type": "integer"}}}}
        result = format_csv([row])
        cell = result.split("\r\n")[1]
        unwrapped = cell[1:-1].replace('""', '"')
        assert json.loads(unwrapped) == row["schema"]
        assert "'" not in cell, "must not emit Python repr"

    def test_nested_array_serializes_as_canonical_json(self) -> None:
        row = {"tags": ["math", "arith"]}
        result = format_csv([row])
        cell = result.split("\r\n")[1]
        unwrapped = cell[1:-1].replace('""', '"')
        assert json.loads(unwrapped) == ["math", "arith"]

    def test_nested_json_is_compact(self) -> None:
        row = {"data": {"a": 1, "b": 2}}
        result = format_csv([row])
        cell = result.split("\r\n")[1]
        assert ", " not in cell  # no space after comma in canonical JSON
        assert ": " not in cell

    def test_unicode_in_nested_object_not_ascii_escaped(self) -> None:
        row = {"label": {"zh": "\u4e2d\u6587"}}
        result = format_csv([row])
        assert "\u4e2d\u6587" in result
        assert "\\u" not in result


class TestFormatCsvRfc4180:
    def test_comma_in_value_quoted(self) -> None:
        result = format_csv([{"a": "x,y"}])
        assert result == 'a\r\n"x,y"\r\n'

    def test_double_quote_doubled(self) -> None:
        result = format_csv([{"a": 'she said "hi"'}])
        assert result == 'a\r\n"she said ""hi"""\r\n'

    def test_newline_in_value_quoted(self) -> None:
        result = format_csv([{"a": "line1\nline2"}])
        assert result == 'a\r\n"line1\nline2"\r\n'

    def test_cr_in_value_quoted(self) -> None:
        result = format_csv([{"a": "line1\rline2"}])
        assert result == 'a\r\n"line1\rline2"\r\n'

    def test_value_without_special_chars_not_quoted(self) -> None:
        result = format_csv([{"a": "plain"}])
        assert result == "a\r\nplain\r\n"


class TestFormatCsvScalarTypes:
    def test_none_emits_empty_cell(self) -> None:
        result = format_csv([{"a": None, "b": 1}])
        assert result == "a,b\r\n,1\r\n"

    def test_bool_lowercase(self) -> None:
        result = format_csv([{"a": True, "b": False}])
        assert result == "a,b\r\ntrue,false\r\n"

    def test_int(self) -> None:
        result = format_csv([{"n": 42}])
        assert result == "n\r\n42\r\n"

    def test_float_whole_renders_as_int(self) -> None:
        """Cross-SDK parity: JS/Rust default number stringification drops .0."""
        result = format_csv([{"n": 1.0}])
        assert result == "n\r\n1\r\n"

    def test_float_fractional(self) -> None:
        result = format_csv([{"n": 1.5}])
        assert result == "n\r\n1.5\r\n"

    def test_nan_and_inf_emit_empty(self) -> None:
        result = format_csv([{"a": float("nan"), "b": float("inf")}])
        assert result == "a,b\r\n,\r\n"

    def test_string_preserved(self) -> None:
        result = format_csv([{"s": "hello"}])
        assert result == "s\r\nhello\r\n"


class TestFormatCsvBom:
    def test_bom_off_by_default(self) -> None:
        result = format_csv([{"a": 1}])
        assert not result.startswith("\ufeff")

    def test_bom_prepended_when_enabled(self) -> None:
        result = format_csv([{"a": 1}], bom=True)
        assert result.startswith("\ufeff")
        assert result[1:] == "a\r\n1\r\n"


class TestFormatJsonl:
    def test_empty_input(self) -> None:
        assert format_jsonl([]) == ""

    def test_single_row(self) -> None:
        result = format_jsonl([{"a": 1, "b": 2}])
        assert result == '{"a":1,"b":2}\n'

    def test_multiple_rows(self) -> None:
        result = format_jsonl([{"a": 1}, {"b": 2}])
        assert result == '{"a":1}\n{"b":2}\n'

    def test_uses_lf_not_crlf(self) -> None:
        result = format_jsonl([{"a": 1}])
        assert "\r" not in result
        assert result.endswith("\n")

    def test_no_trailing_blank_line(self) -> None:
        result = format_jsonl([{"a": 1}, {"b": 2}])
        assert not result.endswith("\n\n")

    def test_compact_no_spaces(self) -> None:
        result = format_jsonl([{"a": 1, "b": 2}])
        assert ", " not in result
        assert ": " not in result

    def test_nested_value_preserved(self) -> None:
        row = {"schema": {"type": "object", "properties": {"a": {"type": "integer"}}}}
        result = format_jsonl([row])
        assert json.loads(result.rstrip("\n")) == row

    def test_unicode_not_escaped(self) -> None:
        row = {"label": "\u4e2d\u6587"}
        result = format_jsonl([row])
        assert "\u4e2d\u6587" in result
        assert "\\u" not in result

    def test_insertion_order_preserved(self) -> None:
        row = {"z": 1, "a": 2, "m": 3}
        result = format_jsonl([row])
        assert result == '{"z":1,"a":2,"m":3}\n'


class TestFormatCsvLargeNumeric:
    def test_large_int_preserved(self) -> None:
        result = format_csv([{"n": 12345678901234567890}])
        assert "12345678901234567890" in result

    def test_negative_int(self) -> None:
        result = format_csv([{"n": -42}])
        assert result == "n\r\n-42\r\n"


class TestFormatCsvJsonWithSpecialChars:
    def test_nested_json_containing_comma_is_quoted(self) -> None:
        row = {"obj": {"a": 1, "b": 2}}
        result = format_csv([row])
        line2 = result.split("\r\n")[1]
        assert line2.startswith('"') and line2.endswith('"')

    def test_nested_json_containing_quote_is_doubled(self) -> None:
        row = {"obj": {"k": 'he said "hi"'}}
        result = format_csv([row])
        cell = result.split("\r\n")[1]
        assert '""""' in cell or '""' in cell


def test_float_canonicalization_internal() -> None:
    """Cross-language parity assertions on number rendering."""
    from apcore_toolkit.formatting.tabular import _canonical_number

    assert _canonical_number(1.0) == "1"
    assert _canonical_number(0.0) == "0"
    assert _canonical_number(-1.0) == "-1"
    assert _canonical_number(1.5) == "1.5"
    assert _canonical_number(-2.25) == "-2.25"
