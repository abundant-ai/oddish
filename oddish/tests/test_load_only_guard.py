"""Keep delivery QA projections covered by the same guard as task lists."""

import importlib.util
import sys
from pathlib import Path

import pytest

_GUARD_PATH = Path(__file__).resolve().parents[1] / "scripts" / "load_only_guard.py"
_spec = importlib.util.spec_from_file_location("load_only_guard", _GUARD_PATH)
guard = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = guard
_spec.loader.exec_module(guard)


def test_registered_queries_cover_builder_reads():
    assert guard.find_violations() == {}
    assert guard.find_stray_load_only_sites() == []


@pytest.mark.parametrize("column", ["harbor_config", "reward"])
def test_delivery_qa_missing_column_is_rejected(tmp_path, monkeypatch, column):
    # harbor_config is read by the evaluator; reward by its imported evidence
    # serializer. Both modules must participate in the column check.
    name, query_path, builders = next(
        unit for unit in guard._COVERAGE_UNITS if unit[0] == "delivery_qa_statuses"
    )
    query_copy = tmp_path / query_path.name
    original = query_path.read_text()
    missing_column = original.replace(f"TrialModel.{column},", "")
    assert missing_column != original
    query_copy.write_text(missing_column)
    monkeypatch.setattr(guard, "_COVERAGE_UNITS", ((name, query_copy, builders),))

    assert guard.find_violations() == {name: {"TrialModel": [column]}}
