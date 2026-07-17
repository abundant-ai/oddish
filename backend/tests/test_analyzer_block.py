import logging

from api.services.analyzer_block import (
    AnalyzerType,
    AnalyzerInput,
    AnalyzerOutput,
    block_key_prefix,
    block_logger,
)


def test_key_prefix_uses_enum_value():
    assert block_key_prefix(AnalyzerType.HEADROOM_ANALYSIS) == "analyzer/headroom_analysis"


def test_io_dataclasses_accept_any():
    assert AnalyzerInput(input={"a": 1}).input == {"a": 1}
    assert AnalyzerOutput(output="text").output == "text"


def test_block_logger_prepends_prefix(caplog):
    log = block_logger("analyzer/scaling_analysis")
    with caplog.at_level(logging.INFO):
        log.info("hello")
    assert "[analyzer/scaling_analysis] hello" in caplog.text
