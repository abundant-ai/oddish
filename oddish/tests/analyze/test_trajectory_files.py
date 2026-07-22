import json
from pathlib import Path

from oddish.analyze.trajectory_files import parse_trajectory_file_access, TrajectoryFileAccess


def _write_log(dir_: Path, events: list[dict]) -> None:
    (dir_ / "claude-code.txt").write_text("\n".join(json.dumps(e) for e in events))


def _assistant(tool, inp):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": tool, "input": inp}]}}


def test_extracts_reads_writes_and_commands(tmp_path):
    _write_log(tmp_path, [
        _assistant("Read", {"file_path": "verifier.py"}),
        _assistant("Edit", {"file_path": "solution.py"}),
        _assistant("Glob", {"pattern": "tests/**"}),
        _assistant("Bash", {"command": "grep -n foo verifier.py"}),
    ])
    steps = parse_trajectory_file_access(tmp_path)
    assert isinstance(steps[0], TrajectoryFileAccess)
    assert steps[0].files_read == ["verifier.py"]
    assert steps[1].files_written == ["solution.py"]
    assert steps[2].files_read == ["tests/**"]  # Glob pattern treated as a read target
    assert steps[3].commands == ["grep -n foo verifier.py"]
    assert [s.step_index for s in steps] == [1, 2, 3, 4]


def test_missing_log_returns_empty(tmp_path):
    assert parse_trajectory_file_access(tmp_path) == []
