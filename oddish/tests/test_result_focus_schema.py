import pytest

from oddish.core.result_focus_schema import (
    UnsupportedSchemaError,
    normalize_findings_schema,
    parse_result_focus,
)


def test_parse_detects_object_schema():
    assert parse_result_focus('{"type": "object", "properties": {}}') == {
        "type": "object",
        "properties": {},
    }


@pytest.mark.parametrize("value", [None, "", "  ", "What broke?", "[1,2]", "42"])
def test_parse_treats_non_object_as_prose(value):
    assert parse_result_focus(value) is None


def test_normalize_inserts_additional_properties_false():
    out = normalize_findings_schema(
        {"type": "object", "properties": {"v": {"type": "string"}}}
    )
    assert out["additionalProperties"] is False
    # nested objects too
    nested = normalize_findings_schema(
        {
            "type": "object",
            "properties": {"inner": {"type": "object", "properties": {}}},
        }
    )
    assert nested["properties"]["inner"]["additionalProperties"] is False


@pytest.mark.parametrize(
    "spec",
    [
        {"type": "object", "properties": {"s": {"type": "string", "minLength": 1}}},
        {"type": "object", "properties": {"n": {"type": "integer", "minimum": 0}}},
        {"type": "object", "properties": {"a": {"type": "array", "minItems": 1}}},
        {"type": "object", "properties": {"r": {"$ref": "#"}}},
    ],
)
def test_normalize_rejects_unsupported(spec):
    with pytest.raises(UnsupportedSchemaError):
        normalize_findings_schema(spec)


def test_normalize_does_not_mutate_input():
    spec = {"type": "object", "properties": {}}
    normalize_findings_schema(spec)
    assert "additionalProperties" not in spec


def test_normalize_rejects_malformed_schema():
    # `type` must be a string/list, not an int — not a valid JSON Schema at all.
    with pytest.raises(UnsupportedSchemaError):
        normalize_findings_schema({"type": 5})
