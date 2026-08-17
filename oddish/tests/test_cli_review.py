"""CLI contract for the read-only ``oddish review`` command."""

from __future__ import annotations

import copy
import json
from unittest.mock import patch

import httpx
import typer
from typer.testing import CliRunner

from oddish.cli.review import review
from oddish.schemas import TaskReviewResponse


def _finding(finding_id: str, tier: str, title: str) -> dict:
    return {
        "id": finding_id,
        "source": "pre_trial",
        "problem_type": "mismatch",
        "dimension": "verifier",
        "file": "scripts/check.sh",
        "line_start": 41,
        "line_end": 48,
        "title": title,
        "detail": f"Exact stored detail for {finding_id}.",
        "recommendation": f"Exact stored repair for {finding_id}.",
        "tier": tier,
        "links_to": None,
        "exploited": finding_id == "finding-must",
        "exploit_evidence": "trajectory step 19" if finding_id == "finding-must" else None,
        "causal": finding_id == "finding-must",
        "from_pre_trial": True,
        "trial_ids": ["trial-model"] if finding_id == "finding-must" else [],
        "experiment_ids": ["exp-1"] if finding_id == "finding-must" else [],
    }


def _trial(trial_id: str, role: str, reward: float, analysis: dict | None) -> dict:
    return {
        "id": trial_id,
        "role": role,
        "experiment_id": "exp-1",
        "agent": "codex" if role == "model" else role,
        "model": "openai/gpt-5.6" if role == "model" else "default",
        "config_fingerprint": f"sha256:{trial_id}",
        "environment": "docker",
        "harbor_sha": "15c40ac",
        "status": "success",
        "reward": reward,
        "cost_usd": 0.41 if role == "model" else 0,
        "duration_seconds": 237.2,
        "included_in_result_run": role == "model",
        "result_run_analysis_fingerprint": (
            "sha256:analysis" if role == "model" else None
        ),
        "analysis_matches_result_run": True if role == "model" else None,
        "analysis_status": "success" if role == "model" else None,
        "analysis": analysis,
    }


_ANALYSIS = {
    "classification": "GOOD_FAILURE",
    "subtype": "Wrong Approach",
    "evidence": "The model chose the wrong file.",
    "root_cause": "The task is sound.",
    "recommendation": "N/A",
    "action_items": [],
    "exploitation": [],
}
_MUST = _finding(
    "finding-must", "must_fix", "Verifier trusts a caller-controlled marker"
)
_SHOULD = _finding(
    "finding-should", "should_fix", "Instruction could state the retention window"
)
_NOP = _trial("trial-nop", "nop", 0, None)
_ORACLE = _trial("trial-oracle", "oracle", 1, None)
_MODEL = _trial("trial-model", "model", 0, _ANALYSIS)


def _payload(
    *,
    findings: list[dict],
    trials: list[dict],
    finding_more: bool = False,
    finding_cursor: str | None = None,
    trial_more: bool = False,
    trial_cursor: str | None = None,
    tiers: list[str] | None = None,
    filtered_total: int = 2,
    legacy: bool = False,
) -> dict:
    return {
        "schema_version": 1,
        "task": {
            "id": "task-1",
            "name": "log rotation",
            "version": 18,
            "version_id": "task-1-v18",
            "content_hash": "d07f61a",
        },
        "scope": {
            "experiment_id": "exp-1",
            "tiers": tiers or ["must_fix", "should_fix"],
            "same_version_across_experiments": False,
        },
        "qa": {
            "status": None if legacy else "success",
            "result_run": (
                None
                if legacy
                else {
                    "id": "qa-1",
                    "disposition": "published",
                    "task_version_id": "task-1-v18",
                    "worker_job_id": "job-1",
                    "input_trial_count": 1,
                    "input_set_sha256": "sha256:inputs",
                    "input_analysis_changed_count": 0,
                    "pre_trial_block_id": "block-a",
                    "verdict_block_id": "block-b",
                    "started_at": "2026-08-13T20:14:02Z",
                    "finished_at": "2026-08-13T20:19:17Z",
                }
            ),
            "active_run": None,
            "is_task_published_run": not legacy,
            "legacy_unscoped_verdict_available": legacy,
            "input_analysis_changed_after_run": False,
        },
        "baselines": {
            "outcome": "valid",
            "nop": {
                "expected_reward": 0,
                "valid": True,
                "trial_count": 1,
                "unexpected_count": 0,
            },
            "oracle": {
                "expected_reward": 1,
                "valid": True,
                "trial_count": 1,
                "unexpected_count": 0,
            },
        },
        "verdict": (
            None
            if legacy
            else {
                "verdict": "reject",
                "is_good": False,
                "confidence": "high",
                "primary_issue": "Verifier bypass remains.",
                "reasoning": "Stored reasoning.",
                "recommendations": ["Tighten the verifier."],
                "task_problem_count": 1,
                "agent_problem_count": 1,
                "success_count": 0,
                "harness_error_count": 0,
            }
        ),
        "finding_counts": {
            "unfiltered_total": 2,
            "filtered_total": filtered_total,
            "must_fix": 1,
            "should_fix": 1,
            "optional": 0,
        },
        "findings": findings,
        "findings_page": {
            "has_more": finding_more,
            "next_cursor": finding_cursor,
        },
        "trial_counts": {
            "eligible": 1,
            "analyzed": 1,
            "unanalyzed": 0,
            "classifications": {
                "GOOD_FAILURE": 1,
                "BAD_FAILURE": 0,
                "GOOD_SUCCESS": 0,
                "BAD_SUCCESS": 0,
                "HARNESS_ERROR": 0,
            },
        },
        "trials": trials,
        "trials_page": {
            "has_more": trial_more,
            "next_cursor": trial_cursor,
        },
    }


def _invoke(handler, args: list[str]):
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def client_factory(*client_args, **client_kwargs):
        client_kwargs["transport"] = transport
        return real_client(*client_args, **client_kwargs)

    app = typer.Typer()
    app.command()(review)
    with (
        patch("oddish.cli.api.httpx.Client", side_effect=client_factory),
        patch("oddish.cli.api.get_auth_headers", return_value={}),
        patch("oddish.cli.review.require_api_key", return_value="test-key"),
    ):
        return CliRunner().invoke(app, args)


def test_review_json_forwards_filters_and_aggregates_independent_pages():
    calls: list[list[tuple[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(list(request.url.params.multi_items()))
        assert request.method == "GET"
        assert request.url.path == "/tasks/log rotation/review"
        params = request.url.params
        if params.get("finding_cursor") == "find-next":
            payload = _payload(findings=[_SHOULD], trials=[])
        elif params.get("trial_cursor") == "trial-next":
            payload = _payload(findings=[], trials=[_ORACLE, _MODEL])
        else:
            payload = _payload(
                findings=[_MUST],
                trials=[_NOP],
                finding_more=True,
                finding_cursor="find-next",
                trial_more=True,
                trial_cursor="trial-next",
            )
        return httpx.Response(200, json=payload)

    result = _invoke(
        handler,
        [
            "log rotation",
            "--version",
            "18",
            "--experiment",
            "exp-1",
            "--tier",
            "must_fix",
            "--tier",
            "should_fix",
            "--api",
            "https://example.test",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert [finding["id"] for finding in document["findings"]] == [
        "finding-must",
        "finding-should",
    ]
    assert [trial["id"] for trial in document["trials"]] == [
        "trial-nop",
        "trial-oracle",
        "trial-model",
    ]
    assert document["findings_page"] == {"has_more": False, "next_cursor": None}
    assert document["trials_page"] == {"has_more": False, "next_cursor": None}
    assert len(calls) == 3
    assert calls[0].count(("tier", "must_fix")) == 1
    assert calls[0].count(("tier", "should_fix")) == 1
    assert ("version", "18") in calls[0]
    assert ("experiment_id", "exp-1") in calls[0]
    assert ("trial_limit", "0") in calls[1]
    assert ("finding_limit", "0") in calls[2]


def test_review_terminal_preserves_prose_and_separates_verifier_from_qa():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_payload(
                findings=[_MUST, _SHOULD], trials=[_NOP, _ORACLE, _MODEL]
            ),
        )

    result = _invoke(handler, ["task-1", "--api", "https://example.test"])

    assert result.exit_code == 0, result.output
    assert _MUST["title"] in result.output
    assert _MUST["detail"] in result.output
    assert _MUST["recommendation"] in result.output
    assert "Verifier ✗ reward 0" in result.output
    assert "QA good failure" in result.output
    assert "No analysis was run by this command." in result.output
    assert "\x1b[" not in result.output


def test_review_fail_on_findings_uses_exit_two_after_rendering():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_payload(
                findings=[_MUST, _SHOULD], trials=[_NOP, _ORACLE, _MODEL]
            ),
        )

    result = _invoke(
        handler,
        ["task-1", "--api", "https://example.test", "--fail-on-findings"],
    )

    assert result.exit_code == 2
    assert _MUST["title"] in result.output


def test_review_fail_on_findings_succeeds_when_selected_scope_is_empty():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_payload(
                findings=[],
                trials=[_NOP, _ORACLE, _MODEL],
                tiers=["optional"],
                filtered_total=0,
            ),
        )

    result = _invoke(
        handler,
        [
            "task-1",
            "--api",
            "https://example.test",
            "--tier",
            "optional",
            "--fail-on-findings",
        ],
    )

    assert result.exit_code == 0, result.output


def test_review_warns_for_legacy_verdict_with_exact_retry_command():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_payload(
                findings=[_MUST, _SHOULD],
                trials=[_NOP, _ORACLE, _MODEL],
                legacy=True,
            ),
        )

    result = _invoke(handler, ["task-1", "--api", "https://example.test"])

    assert result.exit_code == 0, result.output
    assert "predates version-owned QA provenance" in result.output
    assert "oddish run task-1 --retry --qa" in result.output
    assert "No version-scoped verdict is available." in result.output


def test_review_http_and_malformed_response_fail_operationally():
    def forbidden(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "forbidden"})

    forbidden_result = _invoke(
        forbidden, ["task-1", "--api", "https://example.test", "--json"]
    )
    assert forbidden_result.exit_code == 1
    assert "forbidden" in forbidden_result.output

    def malformed(_request: httpx.Request) -> httpx.Response:
        payload = copy.deepcopy(
            _payload(findings=[_MUST, _SHOULD], trials=[_NOP, _ORACLE, _MODEL])
        )
        del payload["task"]["version_id"]
        return httpx.Response(200, json=payload)

    malformed_result = _invoke(
        malformed, ["task-1", "--api", "https://example.test", "--json"]
    )
    assert malformed_result.exit_code == 1
    assert "Malformed review response" in malformed_result.output


def test_review_response_fixture_itself_is_strict_and_complete():
    payload = _payload(
        findings=[_MUST, _SHOULD], trials=[_NOP, _ORACLE, _MODEL]
    )
    response = TaskReviewResponse.model_validate(payload)

    assert response.model_dump(mode="json") == payload


def _active_payload() -> dict:
    """A review page whose QA pass is still running."""
    payload = copy.deepcopy(
        _payload(findings=[], trials=[], filtered_total=0)
    )
    payload["qa"]["status"] = "running"
    payload["qa"]["result_run"] = None
    # No trials have entered the active pass yet; keep the exact totals
    # consistent so the post-timeout full fetch validates.
    payload["trial_counts"]["eligible"] = 0
    payload["baselines"]["nop"]["trial_count"] = 0
    payload["baselines"]["oracle"]["trial_count"] = 0
    payload["qa"]["active_run"] = {
        "id": "qa-active",
        "disposition": None,
        "task_version_id": "task-1-v18",
        "worker_job_id": "job-2",
        "input_trial_count": 0,
        "input_set_sha256": "sha256:pending",
        "input_analysis_changed_count": 0,
        "pre_trial_block_id": None,
        "verdict_block_id": None,
        "started_at": None,
        "finished_at": None,
    }
    return payload


def test_review_wait_polls_with_zero_limit_pages_until_qa_settles():
    calls: list[list[tuple[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"  # --wait stays read-only
        params = list(request.url.params.multi_items())
        calls.append(params)
        is_light_poll = ("finding_limit", "0") in params and (
            "trial_limit",
            "0",
        ) in params
        if is_light_poll and len(calls) == 1:
            return httpx.Response(200, json=_active_payload())
        return httpx.Response(
            200,
            json=_payload(
                findings=[_MUST],
                trials=[_NOP, _ORACLE, _MODEL],
                filtered_total=1,
            ),
        )

    with patch("time.sleep") as sleep_mock:
        result = _invoke(
            handler,
            ["task-1", "--api", "https://example.test", "--wait", "--json"],
        )

    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["qa"]["status"] == "success"
    assert calls[0].count(("finding_limit", "0")) == 1
    assert calls[0].count(("trial_limit", "0")) == 1
    sleep_mock.assert_called_once()


def test_review_wait_timeout_warns_and_renders_current_state():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json=_active_payload())

    with (
        patch("time.sleep"),
        patch("time.monotonic", side_effect=[0.0, 0.0, 100.0, 100.0]),
    ):
        result = _invoke(
            handler,
            [
                "task-1",
                "--api",
                "https://example.test",
                "--wait",
                "--wait-timeout",
                "10",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "QA is still active after 10s" in result.output
    assert "qa-active" in result.output
