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
            "network_mode": "no-network",
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
    assert meta.network_mode == "no-network"
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
    assert meta.network_mode is None
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


def test_expert_time_hours_upper_bound_is_rejected():
    """le must match Numeric(6, 2)'s max representable value (9999.99), not a
    round 10000 that would pass here and overflow at INSERT."""
    from pydantic import ValidationError

    from oddish.schemas import TaskMetadata

    with pytest.raises(ValidationError):
        TaskMetadata(expert_time_hours=10000)


def test_oversized_topic_tags_list_is_rejected():
    from pydantic import ValidationError

    from oddish.schemas import TaskMetadata

    with pytest.raises(ValidationError):
        TaskMetadata(topic_tags=[f"tag-{i}" for i in range(65)])


def test_oversized_topic_tag_element_is_rejected():
    """Regression guard: max_length on list[str] bounds list length, not each
    element's length -- a single element must also be bounded."""
    from pydantic import ValidationError

    from oddish.schemas import TaskMetadata

    with pytest.raises(ValidationError):
        TaskMetadata(topic_tags=["x" * 200])


def _harbor_parsed_environment(environment_body: str) -> dict:
    """Parse a task.toml [environment] stanza through Harbor's real TaskConfig,
    exactly like ``cli/api.py``'s ``_parse_task_config`` and the backfill
    script do -- NOT a hand-built dict. This is the only way to reproduce the
    legacy-``allow_internet``-dropped-on-normalization defect; a hand-built
    dict with an ``allow_internet`` key bypasses Harbor entirely and can't
    catch it."""
    from harbor.models.task.config import TaskConfig as HarborTaskConfig

    toml = f"""\
version = "1.0"
[environment]
docker_image = "python:3.11"
{environment_body}
"""
    config = HarborTaskConfig.model_validate_toml(toml)
    return config.model_dump(mode="json", exclude_none=True)


def test_legacy_allow_internet_false_normalizes_to_no_network():
    """Regression guard for the whole finding: Harbor drops the legacy
    ``allow_internet`` key during parsing and normalizes it into
    ``network_mode`` instead -- project_task_config must read the field
    Harbor actually keeps, not the one it silently discards."""
    config = _harbor_parsed_environment("allow_internet = false")
    meta = project_task_config(config)
    assert meta.network_mode == "no-network"
    assert meta.allow_internet is False


def test_legacy_allow_internet_true_normalizes_to_public():
    config = _harbor_parsed_environment("allow_internet = true")
    meta = project_task_config(config)
    assert meta.network_mode == "public"
    assert meta.allow_internet is True


def test_explicit_allowlist_network_mode_derives_allow_internet_true():
    """``allowlist`` is partial egress, a genuine third state -- but for the
    "does this task have any egress" question allow_internet answers, it's
    True same as ``public``."""
    config = _harbor_parsed_environment(
        'network_mode = "allowlist"\nallowed_hosts = ["api.anthropic.com"]'
    )
    meta = project_task_config(config)
    assert meta.network_mode == "allowlist"
    assert meta.allow_internet is True


def test_all_zero_category_slugifies_to_none():
    """slugify('---') == '' -- the empty slug must persist as None, not ''."""
    meta = project_task_config({"metadata": {"category": "---"}})
    assert meta.category is None


def test_sweep_and_task_submission_accept_absent_metadata():
    """Old-CLI compatibility for the sweep path (`oddish run`'s upload)."""
    from oddish.schemas import TaskSubmission, TaskSweepSubmission

    submission = TaskSubmission(
        task_path="/tmp/demo", trials=[{"agent": "nop"}]
    )
    assert submission.task_metadata is None
    assert submission.provenance is None

    sweep = TaskSweepSubmission(task_id="demo-1234abcd", configs=[{"agent": "nop"}])
    assert sweep.task_metadata is None
    assert sweep.provenance is None


def test_task_provenance_constructs_with_no_arguments():
    from oddish.schemas import TaskProvenance

    provenance = TaskProvenance()
    assert provenance.source_repo is None
    assert provenance.source_commit is None
    assert provenance.uploader_is_ci is None


def test_task_provenance_oversized_source_repo_is_rejected():
    from pydantic import ValidationError

    from oddish.schemas import TaskProvenance

    with pytest.raises(ValidationError):
        TaskProvenance(source_repo="x" * 256)
