from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.task_metadata import project_task_config, slugify


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ml_training", "ml-training"),
        ("ml-training", "ml-training"),
        ("ML Training", "ml-training"),
        ("  reverse-engineering  ", "reverse-engineering"),
        ("code__translation", "code-translation"),
    ],
)
def test_slugify_collapses_separator_drift(raw, expected):
    assert slugify(raw) == expected


def _config() -> dict:
    return {
        "metadata": {
            "author_name": "Ada",
            "author_email": "ada@abundant.ai",
            "author_organization": "Abundant AI",
            "difficulty": "hard",
            "category": "ml_training",
            "tags": ["compilers", "Rust"],
            "description": "Port CBACT01C.",
            "expert_time_estimate_hours": 12.5,
        },
        "environment": {
            "allow_internet": False,
            "cpus": 4,
            "memory_mb": 16384,
            "storage_mb": 16384,
            "gpus": 1,
            "gpu_types": ["H100"],
            "build_timeout_sec": 1200,
        },
        "agent": {"timeout_sec": 18000},
        "verifier": {"timeout_sec": 2400},
    }


def test_project_task_config_extracts_all_fields():
    meta = project_task_config(_config())
    assert meta.description == "Port CBACT01C."
    assert meta.category == "ml-training"
    assert meta.category_raw == "ml_training"
    assert meta.topic_tags == ["compilers", "rust"]
    assert meta.author_name == "Ada"
    assert meta.author_organization == "Abundant AI"
    assert meta.expert_time_hours == 12.5
    assert meta.allow_internet is False
    assert meta.cpus == 4
    assert meta.memory_mb == 16384
    assert meta.gpus == 1
    assert meta.gpu_types == ["H100"]
    assert meta.agent_timeout_sec == 18000
    assert meta.verifier_timeout_sec == 2400
    assert meta.build_timeout_sec == 1200


def test_project_task_config_tolerates_missing_stanzas():
    meta = project_task_config({})
    assert meta.category is None
    assert meta.topic_tags == []
    assert meta.allow_internet is None


def test_difficulty_is_not_projected():
    # Constant ("hard") across all 156 harbor-lh tasks; deliberately dropped.
    meta = project_task_config(_config())
    assert not hasattr(meta, "difficulty")


def test_all_upload_models_accept_absent_metadata():
    """Old-CLI compatibility across every request path.

    If any of these gains a required metadata field, every CLI in the field
    breaks on its next upload.
    """
    from oddish.schemas import TaskUploadCompleteRequest, TaskUploadInitRequest

    init = TaskUploadInitRequest(name="demo", content_hash="h")
    assert init.task_metadata is None
    assert init.provenance is None

    complete = TaskUploadCompleteRequest(
        task_id="demo-1234abcd", name="demo", version=1, content_hash="h"
    )
    assert complete.task_metadata is None
    assert complete.provenance is None


def test_oversized_description_is_rejected():
    from pydantic import ValidationError

    from oddish.schemas import TaskMetadata

    with pytest.raises(ValidationError):
        TaskMetadata(description="x" * 8193)


def test_negative_cpus_is_rejected():
    from pydantic import ValidationError

    from oddish.schemas import TaskMetadata

    with pytest.raises(ValidationError):
        TaskMetadata(cpus=-1)
