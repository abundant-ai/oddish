from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.cli.api import format_trial_status, format_trial_status_detail


def test_failed_user_cancelled_trial_shows_cancelled_by_user_detail() -> None:
    detail = format_trial_status_detail(
        {
            "status": "failed",
            "harbor_stage": "cancelled",
            "error_message": "Cancelled by user",
        }
    )

    assert detail == "[yellow]cancelled by user[/yellow]"


def test_cancelled_stage_is_visible_without_error_message() -> None:
    detail = format_trial_status_detail(
        {"status": "failed", "harbor_stage": "cancelled", "error_message": None}
    )

    assert detail == "[yellow]cancelled[/yellow]"


def test_failed_trial_detail_uses_escaped_error_message() -> None:
    detail = format_trial_status_detail(
        {
            "status": "failed",
            "harbor_stage": "completed",
            "error_message": "Worker failed [badly]",
        }
    )

    assert detail == "[red]Worker failed \\[badly][/red]"


def test_running_trial_detail_uses_harbor_stage() -> None:
    detail = format_trial_status_detail(
        {"status": "running", "harbor_stage": "agent_running"}
    )

    assert detail == "[blue]agent running[/blue]"


def test_cancelled_status_gets_yellow_status_style() -> None:
    assert format_trial_status("cancelled") == "[yellow]cancelled[/yellow]"
