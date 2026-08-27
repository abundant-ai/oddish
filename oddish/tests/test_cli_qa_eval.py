from __future__ import annotations

import csv

import httpx
from typer.testing import CliRunner

from oddish.cli import app

runner = CliRunner()


class _RunClient:
    posted: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def post(self, url: str, json: dict):
        self.posted.append({"url": url, "json": json})
        index = len(self.posted)
        return httpx.Response(
            200,
            json={
                "experiment_id": f"experiment-{index}",
                "experiment_name": json["name"],
                "prompt_name": json["prompt_name"],
                "prompt_sha256": "abc",
                "model": "deployed-model",
                "trials": [
                    {
                        "source_trial_id": source_id,
                        "qa_eval_trial_id": f"eval-{source_id}-{index}",
                    }
                    for source_id in json["source_trial_ids"]
                ],
            },
            request=httpx.Request("POST", url),
        )


def test_run_submits_each_prompt_without_case_contents(monkeypatch, tmp_path):
    cases = tmp_path / "cases.csv"
    cases.write_text(
        "source_trial_id,researcher_issue\nsource-1,hidden issue\nsource-2,other\n"
    )
    prompt_1 = tmp_path / "one.txt"
    prompt_1.write_text("first candidate")
    prompt_2 = tmp_path / "two.txt"
    prompt_2.write_text("second candidate")
    _RunClient.posted = []
    monkeypatch.setattr("oddish.cli.qa_eval.httpx.Client", _RunClient)
    monkeypatch.setenv("ODDISH_API_KEY", "test-key")

    result = runner.invoke(
        app,
        [
            "qa-eval",
            "run",
            "--cases",
            str(cases),
            "--prompt",
            f"candidate-1={prompt_1}",
            "--prompt",
            f"candidate-2={prompt_2}",
            "--name",
            "feedback",
            "--api",
            "https://api.example",
        ],
    )

    assert result.exit_code == 0, result.output
    assert [row["json"]["name"] for row in _RunClient.posted] == [
        "feedback-candidate-1",
        "feedback-candidate-2",
    ]
    assert _RunClient.posted[0]["json"]["source_trial_ids"] == [
        "source-1",
        "source-2",
    ]
    assert _RunClient.posted[0]["json"]["prompt_text"] == "first candidate"
    assert "researcher_issue" not in _RunClient.posted[0]["json"]


def test_collect_writes_the_requested_columns(monkeypatch, tmp_path):
    labels = tmp_path / "labels.csv"
    labels.write_text(
        "source_trial_id,researcher_issue,researcher_issue_caught\n"
        "source-1,The grader missed the root cause,yes\n"
    )
    out = tmp_path / "comparison.csv"

    def fake_get(url: str, **kwargs):
        return httpx.Response(
            200,
            json={
                "experiment_id": "experiment-1",
                "experiment_name": "feedback",
                "rows": [
                    {
                        "source_trial_id": "source-1",
                        "qa_eval_trial_id": "eval-1",
                        "task_name": "task-one",
                        "status": "success",
                        "historical_qa_classification": "GOOD_FAILURE",
                        "historical_qa_root_cause": "Old cause.",
                        "candidate_qa_classification": "BAD_FAILURE",
                        "candidate_qa_root_cause": "New cause.",
                        "qa_response_valid": True,
                        "failure_stage": None,
                    }
                ],
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("oddish.cli.qa_eval.httpx.get", fake_get)
    monkeypatch.setenv("ODDISH_API_KEY", "test-key")
    result = runner.invoke(
        app,
        [
            "qa-eval",
            "collect",
            "feedback",
            "--labels",
            str(labels),
            "--out",
            str(out),
            "--api",
            "https://api.example",
        ],
    )

    assert result.exit_code == 0, result.output
    with out.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows == [
        {
            "source_trial_id": "source-1",
            "task_name": "task-one",
            "researcher_issue": "The grader missed the root cause",
            "historical_qa_classification": "GOOD_FAILURE",
            "historical_qa_root_cause": "Old cause.",
            "candidate_qa_classification": "BAD_FAILURE",
            "candidate_qa_root_cause": "New cause.",
            "researcher_issue_caught": "yes",
            "qa_response_valid": "true",
            "failure_stage": "",
        }
    ]
