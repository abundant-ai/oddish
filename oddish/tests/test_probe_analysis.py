from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.worker import probe_analysis  # noqa: E402


def test_build_transcript_empty_returns_sentinel():
    assert probe_analysis._build_transcript([]) == (
        "(empty transcript — agent produced no output)"
    )


def test_build_transcript_keeps_full_final_audit():
    """A completed audit's final message (~12KB JSON) must not be clipped to the
    old 1500-char per-line cap, which made the summarizer see it cut off."""
    audit = "X" * 12000  # stand-in for the final audit JSON
    messages = [
        {"kind": "assistant_text", "text": "reading files"},
        {"kind": "result", "text": audit},
    ]
    transcript = probe_analysis._build_transcript(messages)
    assert audit in transcript  # the deliverable survives in full


def test_build_transcript_overlong_keeps_head_and_tail():
    """When the transcript exceeds the budget, the agent's final message (tail)
    must still be present -- a head-only clip hid the conclusion."""
    filler = [{"kind": "tool_result", "text": "f" * 4000} for _ in range(200)]
    final = "FINAL_AUDIT_VERDICT_SOLVABLE_ONLY_BY_GUESSING"
    messages = filler + [{"kind": "result", "text": final}]
    transcript = probe_analysis._build_transcript(messages)
    assert len(transcript) <= probe_analysis._TRANSCRIPT_MAX_CHARS + 200
    assert final in transcript  # tail preserved
    assert "truncated to fit context" in transcript
    assert transcript.startswith("[1] tool_result:")  # head preserved


def _norm(parsed):
    return probe_analysis._normalize_probe_summary(parsed, result_focus="", model="m")


def test_must_fix_capped_when_cheat_did_not_succeed():
    """A must_fix asserts an exploitable-NOW hole; without a successful cheat it
    is capped to should_fix so a non-succeeding run can't surface a must-fix."""
    out = _norm(
        {
            "cheating_succeeded": False,
            "recommendations": [
                {"priority": "must_fix", "action": "lock down /opt", "rationale": "r"}
            ],
        }
    )
    assert out["recommendations"][0]["priority"] == "should_fix"


def test_must_fix_preserved_when_cheat_succeeded():
    out = _norm(
        {
            "cheating_succeeded": True,
            "recommendations": [
                {"priority": "must_fix", "action": "trust no results.json", "rationale": "r"}
            ],
        }
    )
    assert out["recommendations"][0]["priority"] == "must_fix"


def test_soft_gate_keeps_should_fix_without_success():
    """Soft gate: only must_fix is capped; should_fix/optional survive a
    non-succeeding run (e.g. a credible untested-weakness hypothesis)."""
    out = _norm(
        {
            "cheating_succeeded": None,
            "recommendations": [
                {"priority": "should_fix", "action": "narrow the regex", "rationale": "r"},
                {"priority": "optional", "action": "add a canary", "rationale": "r"},
            ],
        }
    )
    assert [r["priority"] for r in out["recommendations"]] == ["should_fix", "optional"]
