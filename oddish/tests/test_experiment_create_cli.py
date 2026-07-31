from oddish.cli._collection_refs import _dedupe
from oddish.cli.experiment import _format_collection_summary


def test_normalize_dedupes_and_strips():
    assert _dedupe([" a ", "b", "a", "", "  "]) == ["a", "b"]


def test_format_summary_lines():
    lines = _format_collection_summary(
        {"id": "exp_x", "name": "c", "trials_linked": 3, "tasks_linked": 2}
    )
    assert any("exp_x" in ln for ln in lines)
    assert any("3" in ln for ln in lines)
