"""Tests for the member-facing profanity-report POST endpoint."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_profanity_report_route_registered():
    import pytest

    tags_router = pytest.importorskip("backend.api.routers.tags")

    paths = {(r.path, sorted(r.methods or [])) for r in tags_router.router.routes}
    assert any(
        p == "/tag-policy/profanity-reports" and "POST" in methods
        for p, methods in paths
    )


def test_profanity_report_request_dto_fields():
    from oddish.schemas import ProfanityReportCreateRequest

    req = ProfanityReportCreateRequest(tag_id="t-1", reason="contains slur")
    assert req.tag_id == "t-1"
    assert req.reason == "contains slur"
