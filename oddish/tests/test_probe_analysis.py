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


def test_normalize_drops_cheating_and_recommendations():
    """The analyzer no longer surfaces a cheat verdict or operator recommendations
    (a probe is adversarial by design, so the cheat framing produced false
    'fix the anti-cheat' action items). The summary keeps what the probe found."""
    out = _norm(
        {
            "headline": "h",
            "summary": "s",
            "key_actions": ["read tests"],
            "hypotheses": ["only filter.json tests are scored"],
            "cheating_attempted": True,
            "cheating_succeeded": True,
            "recommendations": [
                {"priority": "must_fix", "action": "lock down /opt", "rationale": "r"}
            ],
        }
    )
    assert "cheating_attempted" not in out
    assert "cheating_succeeded" not in out
    assert "recommendations" not in out
    assert out["hypotheses"] == ["only filter.json tests are scored"]
