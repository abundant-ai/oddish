from pathlib import Path

import rewardkit as rk
from rewardkit import criterion

rk.file_contains("data/metrics.csv", "cpu_seconds")


@criterion(description="data/metrics.csv has a header and at least two data rows")
def metrics_has_data_rows(workspace: Path) -> bool:
    path = workspace / "data" / "metrics.csv"
    if not path.exists():
        return False
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return len(lines) >= 3
