"""Pydantic-level validation tests for ``ReportSpec``.

End-to-end DB integration tests live separately (require a running
Postgres). These tests cover the authoring contract: invalid specs from
agents should fail loudly *before* the request hits the server.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from oddish.schemas import (
    REPORT_SPEC_VERSION,
    ReportSpec,
    ReportTaskVersionEntry,
)


def _minimal_spec(**overrides) -> dict:
    data: dict = {
        "name": "test",
        "agents": [
            {"id": "A", "agent": "agent-A", "model": "model-X"},
        ],
        "task_versions": [{"task_version_id": "tv1"}],
        "trials_per_cell": 1,
    }
    data.update(overrides)
    return data


def test_minimal_spec_validates() -> None:
    spec = ReportSpec.model_validate(_minimal_spec())
    assert spec.name == "test"
    assert spec.version == REPORT_SPEC_VERSION
    assert spec.trials_per_cell == 1
    assert spec.selection.strategy == "latest"
    assert spec.source.status == ["success", "failed"]


def test_trials_per_cell_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        ReportSpec.model_validate(_minimal_spec(trials_per_cell=0))


def test_duplicate_agent_ids_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        ReportSpec.model_validate(
            _minimal_spec(
                agents=[
                    {"id": "A", "agent": "x", "model": "y"},
                    {"id": "A", "agent": "x2", "model": "y2"},
                ]
            )
        )
    assert "Duplicate agents[].id" in str(exc_info.value)


def test_pin_references_unknown_column_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        ReportSpec.model_validate(
            _minimal_spec(
                task_versions=[
                    {
                        "task_version_id": "tv1",
                        "pins": {"NOT-A-COLUMN": ["trial-1"]},
                    }
                ]
            )
        )
    assert "unknown column" in str(exc_info.value)


def test_task_version_entry_requires_identifier() -> None:
    with pytest.raises(ValidationError):
        ReportTaskVersionEntry.model_validate({})


def test_task_version_entry_version_requires_task_id() -> None:
    with pytest.raises(ValidationError):
        ReportTaskVersionEntry.model_validate(
            {"task_version_id": "tv1", "version": 2}
        )


def test_non_nop_agent_requires_model() -> None:
    with pytest.raises(ValidationError):
        ReportSpec.model_validate(
            _minimal_spec(
                agents=[{"id": "A", "agent": "agent-A"}]  # no model
            )
        )


def test_nop_agent_allowed_without_model() -> None:
    spec = ReportSpec.model_validate(
        _minimal_spec(agents=[{"id": "A", "agent": "nop"}])
    )
    assert spec.agents[0].agent == "nop"
    assert spec.agents[0].model is None


def test_oracle_agent_allowed_without_model() -> None:
    spec = ReportSpec.model_validate(
        _minimal_spec(agents=[{"id": "A", "agent": "oracle"}])
    )
    assert spec.agents[0].agent == "oracle"


def test_selection_strategy_random_seed_optional() -> None:
    spec = ReportSpec.model_validate(
        _minimal_spec(selection={"strategy": "random", "seed": 42})
    )
    assert spec.selection.strategy == "random"
    assert spec.selection.seed == 42


def test_source_filter_defaults_to_empty_scope() -> None:
    spec = ReportSpec.model_validate(_minimal_spec())
    assert spec.source.experiment_ids == []
    assert spec.source.include_superseded is False


def test_yaml_round_trip_through_pydantic() -> None:
    """Spec model_dump produces JSON-safe values that round-trip cleanly."""
    spec = ReportSpec.model_validate(_minimal_spec())
    payload = spec.model_dump(mode="json")
    reloaded = ReportSpec.model_validate(payload)
    assert reloaded == spec
