import pytest

from oddish.core.result_focus_schema import (
    UnsupportedSchemaError,
    normalize_findings_schema,
    parse_result_focus,
)


@pytest.mark.asyncio
async def test_preset_schema_save_normalizes_and_rejects():
    # accepted: object schema → additionalProperties inserted
    spec = await parse_result_focus('{"type":"object","properties":{"v":{"type":"string"}}}')
    assert normalize_findings_schema(spec)["additionalProperties"] is False
    # rejected: unsupported construct
    bad = await parse_result_focus('{"type":"object","properties":{"v":{"type":"string","minLength":1}}}')
    with pytest.raises(UnsupportedSchemaError):
        normalize_findings_schema(bad)
