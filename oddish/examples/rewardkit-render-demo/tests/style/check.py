from pathlib import Path

from rewardkit import criterion


@criterion(description="report.md is a substantial report; 60 or more words earn full credit")
def report_length(workspace: Path) -> float:
    path = workspace / "report.md"
    if not path.exists():
        return 0.0
    words = len(path.read_text(encoding="utf-8").split())
    return min(1.0, words / 60)
