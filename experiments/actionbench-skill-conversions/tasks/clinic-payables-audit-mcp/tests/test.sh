#!/bin/bash

apt-get update
apt-get install -y curl

curl -LsSf https://astral.sh/uv/0.9.7/install.sh | sh

source $HOME/.local/bin/env

mkdir -p /logs/verifier

uvx \
  --with pytest==8.4.1 \
  --with pytest-json-ctrf==0.3.5 \
  pytest --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -rA
pytest_status=$?

python3 - "$pytest_status" <<'PY'
import json
import sys
from pathlib import Path

status = int(sys.argv[1])
ctrf_path = Path("/logs/verifier/ctrf.json")
metrics_path = Path("/logs/verifier/metrics.json")

passed = 0
total = 0
if ctrf_path.exists():
    try:
        summary = json.loads(ctrf_path.read_text()).get("results", {}).get("summary", {})
        passed = int(summary.get("passed") or 0)
        total = int(summary.get("tests") or 0)
    except Exception:
        passed = 0
        total = 0

partial_score = passed / total if total else (1.0 if status == 0 else 0.0)
reward = 1.0 if status == 0 else 0.0
metrics_path.write_text(
    json.dumps(
        {
            "partial_score": partial_score,
            "reward": reward,
            "passed": passed,
            "total": total,
        },
        indent=2,
    )
    + "\n"
)
PY

if [ "$pytest_status" -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
