from api.services.blocks.analyzer.analyzer_block import AnalyzerType


def test_analyzer_type_has_pre_trial():
    assert AnalyzerType.PRE_TRIAL.value == "pre_trial"
