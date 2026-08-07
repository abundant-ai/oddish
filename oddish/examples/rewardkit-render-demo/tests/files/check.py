from pathlib import Path

import rewardkit as rk
from rewardkit import criterion

rk.file_exists("report.md")
rk.file_contains("report.md", "RewardKit render demo", weight=2.0)


@criterion(description="summary.txt has exactly three lines")
def summary_has_three_lines(workspace: Path) -> bool:
    path = workspace / "summary.txt"
    if not path.exists():
        return False
    return len(path.read_text(encoding="utf-8").splitlines()) == 3
