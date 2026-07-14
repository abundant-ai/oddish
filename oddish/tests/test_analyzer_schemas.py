import pytest
from pydantic import ValidationError

from oddish.schemas import AnalyzerCreate, AnalyzerResponse, ExperimentOption


def test_analyzer_create_requires_name_and_experiments():
    c = AnalyzerCreate(name="Q3", experiment_ids=["e1", "e2"])
    assert c.name == "Q3" and c.experiment_ids == ["e1", "e2"]


def test_analyzer_create_rejects_empty_name():
    with pytest.raises(ValidationError):
        AnalyzerCreate(name="", experiment_ids=["e1"])


def test_analyzer_create_rejects_empty_experiment_ids():
    with pytest.raises(ValidationError):
        AnalyzerCreate(name="Q3", experiment_ids=[])


def test_analyzer_response_from_attrs_shape():
    fields = set(AnalyzerResponse.model_fields)
    assert {
        "id", "name", "status", "error",
        "bad_failure_content", "good_failure_content",
        "universal_capabilities_content", "headroom_analysis",
        "num_trials", "num_bad_failures", "num_good_failures",
        "breakdown", "experiment_ids", "created_at", "finished_at",
    } <= fields


def test_experiment_option():
    o = ExperimentOption(id="e1", name="exp one")
    assert o.model_dump() == {"id": "e1", "name": "exp one"}
