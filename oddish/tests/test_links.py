"""Tests for the canonical dashboard address builders (oddish.links)."""

from __future__ import annotations

import pytest

from oddish.links import (
    experiment_url,
    format_lines,
    task_url,
    trial_task_id,
)


class TestFormatLines:
    def test_int_single_line(self):
        assert format_lines(12) == "L12"

    def test_tuple_range(self):
        assert format_lines((12, 20)) == "L12-L20"

    def test_tuple_reversed_swaps(self):
        assert format_lines((20, 12)) == "L12-L20"

    def test_tuple_collapsed_to_single(self):
        assert format_lines((7, 7)) == "L7"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("12", "L12"),
            ("12-20", "L12-L20"),
            ("L12", "L12"),
            ("L12-L20", "L12-L20"),
            ("l12-20", "L12-L20"),
            (" 12-20 ", "L12-L20"),
        ],
    )
    def test_lenient_strings(self, raw, expected):
        assert format_lines(raw) == expected

    @pytest.mark.parametrize("raw", ["", "abc", "L", "12-", "1.5", "-3"])
    def test_malformed_strings_raise(self, raw):
        with pytest.raises(ValueError):
            format_lines(raw)

    def test_zero_line_raises(self):
        with pytest.raises(ValueError):
            format_lines(0)


class TestTrialTaskId:
    def test_derives_task_id(self):
        assert (
            trial_task_id("pandas-memory-optimization-colab-61bb5824-350")
            == "pandas-memory-optimization-colab-61bb5824"
        )

    def test_non_numeric_suffix_returns_none(self):
        assert trial_task_id("no-numeric-suffix") is None

    def test_no_separator_returns_none(self):
        assert trial_task_id("350") is None


class TestTaskUrl:
    def test_bare_task(self):
        assert task_url("my-task") == "/tasks/my-task"

    def test_base_url_and_version(self):
        # The dashboard resolves ?version= against version row ids
        # ({task_id}-v{n}), so a bare number is expanded to the id form.
        assert (
            task_url("my-task", base_url="https://www.oddish.app/", version=3)
            == "https://www.oddish.app/tasks/my-task?version=my-task-v3"
        )

    def test_version_accepts_full_id(self):
        assert (
            task_url("my-task", version="my-task-v3")
            == "/tasks/my-task?version=my-task-v3"
        )

    def test_trial_with_file_and_lines(self):
        url = task_url(
            "my-task",
            trial_id="my-task-3",
            tab="files",
            file="verifier/test-stdout.txt",
            lines="40-52",
        )
        assert url == (
            "/tasks/my-task?trial=my-task-3&tab=files"
            "&file=verifier%2Ftest-stdout.txt&lines=L40-L52"
        )

    def test_task_file_without_trial_marks_drawer(self):
        url = task_url("my-task", task_file="solution/solve.sh", task_lines=3)
        assert url == (
            "/tasks/my-task?drawer=task&taskFile=solution%2Fsolve.sh"
            "&taskLines=L3"
        )

    def test_trial_pane_params_require_trial(self):
        with pytest.raises(ValueError):
            task_url("my-task", file="a.py")

    def test_task_lines_require_task_file(self):
        with pytest.raises(ValueError):
            task_url("my-task", task_lines=3)

    def test_lines_require_file(self):
        with pytest.raises(ValueError):
            task_url("my-task", trial_id="my-task-3", lines="12-20")

    def test_file_requires_a_file_tab(self):
        # The panel only keeps ?file=/?lines= on the files/artifacts tabs;
        # minting them with another tab would produce an unstable address.
        with pytest.raises(ValueError):
            task_url("my-task", trial_id="my-task-3", tab="summary", file="a.py")

    def test_file_allowed_on_artifacts_tab(self):
        url = task_url(
            "my-task", trial_id="my-task-3", tab="artifacts", file="logs/a.log"
        )
        assert url == (
            "/tasks/my-task?trial=my-task-3&tab=artifacts&file=logs%2Fa.log"
        )


class TestExperimentUrl:
    def test_trial_only_is_complete(self):
        assert (
            experiment_url("leg-d897144a32c2", trial_id="t-1")
            == "/experiments/leg-d897144a32c2?trial=t-1"
        )

    def test_full_address(self):
        url = experiment_url(
            "leg-d897144a32c2",
            base_url="https://www.oddish.app",
            task_id="my-task",
            trial_id="my-task-3",
            tab="files",
            file="a.py",
            lines=(12, 20),
            task_file="instruction.md",
            task_lines="8-14",
        )
        assert url == (
            "https://www.oddish.app/experiments/leg-d897144a32c2"
            "?task=my-task&trial=my-task-3&tab=files&file=a.py"
            "&lines=L12-L20&taskFile=instruction.md&taskLines=L8-L14"
        )

    def test_task_pane_params_need_task_or_trial(self):
        with pytest.raises(ValueError):
            experiment_url("exp", task_file="instruction.md")

    def test_lines_require_file(self):
        with pytest.raises(ValueError):
            experiment_url("exp", trial_id="t-1", lines=12)
