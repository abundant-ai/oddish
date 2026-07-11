from oddish.schemas import ReportCreate, ReportResponse, ExperimentOption


def test_report_create_requires_name_and_experiments():
    c = ReportCreate(name="Q3", experiment_ids=["e1", "e2"])
    assert c.name == "Q3" and c.experiment_ids == ["e1", "e2"]


def test_report_response_from_attrs_shape():
    fields = set(ReportResponse.model_fields)
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
