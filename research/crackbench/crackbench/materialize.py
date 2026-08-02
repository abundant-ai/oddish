"""Materialize a ``GeneratedTask`` into a Harbor task directory.

Layout written (matches Harbor / oddish conventions):

    <dest>/
    ├── task.toml
    ├── instruction.md
    ├── environment/Dockerfile
    ├── solution/README.md
    └── tests/
        ├── checkpoints.json
        └── test.sh          # dense-reward verifier -> /logs/verifier/reward.txt

The verifier runs each checkpoint's ``verify_cmd`` and writes a weighted-fraction
reward plus ``metrics.json`` and ``ctrf.json``, exactly the dense, partial-credit
grading the design calls for. The environment is a scaffold: full task realization
(shipping the real symbol-poor binary and oracle) is intentionally left for later,
alongside the deferred checkpoint DAG.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from .models import GeneratedTask

_GENERATED_BY = "crackbench-auto-research"


def _toml_str(value: object) -> str:
    """Return a valid TOML basic string for arbitrary (LLM-controlled) text.

    json.dumps yields the same ``"..."`` quoting and the same ``\\"`` / ``\\\\`` /
    ``\\n`` / ``\\t`` / ``\\uXXXX`` escapes TOML accepts, so a value containing a
    quote or newline can't break the rendered task.toml.
    """
    return json.dumps(str(value))


def render_task_toml(task: GeneratedTask, *, long_horizon_minutes: float) -> str:
    # Give the agent a ceiling comfortably above the long-horizon threshold so a
    # genuinely long task is not cut off before it can finish.
    agent_timeout_sec = int(math.ceil((long_horizon_minutes + 30) * 60))
    lines = [
        'version = "1.0"',
        "",
        "[task]",
        f"name = {_toml_str(f'crackbench/{task.slug}')}",
        "",
        # Harbor/oddish read a top-level [metadata] table (TaskConfig.metadata),
        # not [task.metadata]; nesting it under [task] drops these fields.
        "[metadata]",
        f"category = {_toml_str(task.category)}",
        f"difficulty = {_toml_str(task.difficulty)}",
        f"long_horizon_target_minutes = {int(long_horizon_minutes)}",
        f"generated_by = {_toml_str(_GENERATED_BY)}",
        f"techniques = {json.dumps([str(t) for t in task.techniques])}",
        "",
        "[environment]",
        "# Harbor builds environment/Dockerfile for this task.",
        # CrackMeBench tasks are self-contained: no egress. Harbor defaults
        # network_mode to "public", and oddish's closed_internet preflight flags
        # that, so declare no-network explicitly.
        'network_mode = "no-network"',
        "build_timeout_sec = 1800",
        "",
        "[agent]",
        # oddish validate_task_timeout_config requires [agent].timeout_sec.
        f"timeout_sec = {agent_timeout_sec}",
        "",
        "[verifier]",
        "timeout_sec = 900",
        "",
    ]
    return "\n".join(lines)


def render_dockerfile(task: GeneratedTask) -> str:
    base = task.environment.get("base_image", "ubuntu:24.04")
    packages = task.environment.get("packages", []) or []
    apt = [p for p in packages if p not in {"pwntools"}]
    pip = [p for p in packages if p in {"pwntools"}]
    # The generated verifier (tests/test.sh) runs a python3 heredoc, so python3
    # must always be present even if the task itself declares no packages;
    # ubuntu:24.04 ships none by default.
    apt = apt + ["python3"]
    if pip:
        # pip packages also need pip; ubuntu:24.04 is PEP 668 externally-managed
        # (hence --break-system-packages below).
        apt = apt + ["python3-pip"]
    lines = [
        f"FROM {base}",
        "ENV DEBIAN_FRONTEND=noninteractive",
    ]
    if apt:
        pkg_str = " ".join(sorted(set(apt)))
        lines += [
            "RUN apt-get update \\",
            f"    && apt-get install -y --no-install-recommends {pkg_str} \\",
            "    && rm -rf /var/lib/apt/lists/*",
        ]
    if pip:
        lines.append(
            "RUN pip3 install --no-cache-dir --break-system-packages "
            + " ".join(sorted(set(pip)))
        )
    lines += [
        "WORKDIR /workspace",
        "# TODO(full-realization): COPY the symbol-poor challenge binary / assets here.",
        'CMD ["/bin/bash"]',
        "",
    ]
    return "\n".join(lines)


def render_instruction(task: GeneratedTask) -> str:
    cps = "\n".join(
        f"{i + 1}. **{cp.title}** — {cp.description}"
        for i, cp in enumerate(task.checkpoints)
    )
    return (
        f"# {task.title}\n\n"
        f"{task.summary}\n\n"
        f"{task.instruction.strip()}\n\n"
        "## Rules\n\n"
        "- The environment has no network access.\n"
        "- Your work is scored by an automated verifier against the checkpoints "
        "below; explanations are not scored.\n\n"
        "## Checkpoints\n\n"
        f"{cps}\n"
    )


def render_solution(task: GeneratedTask) -> str:
    return (
        f"# Reference solution — {task.title}\n\n"
        "This directory holds the ground truth. It is never exposed to the agent.\n\n"
        "## Approach\n\n"
        f"{task.solution_notes or '(fill in the reference walkthrough)'}\n\n"
        "## Checkpoint map\n\n"
        + "\n".join(
            f"- `{cp.id}` (weight {cp.weight}, depends_on={cp.depends_on}): "
            f"`{cp.verify_cmd}`"
            for cp in task.checkpoints
        )
        + "\n\n> TODO(full-realization): add solve.sh that trips every checkpoint.\n"
    )


_VERIFIER_TEMPLATE = r'''#!/usr/bin/env bash
# Dense-reward checkpoint verifier generated by crackbench.
# Writes weighted reward in [0,1] to /logs/verifier/reward.txt plus metrics.json
# and ctrf.json. Checkpoints read state only; a missing artifact fails closed.
set -u
# Harbor reads the reward from /logs/verifier; the override only exists so the
# harness's own tests can run this verifier in a scratch dir.
OUT="${CRACKBENCH_VERIFIER_OUT:-/logs/verifier}"
mkdir -p "$OUT"
HERE="$(cd "$(dirname "$0")" && pwd)"

python3 - "$HERE/checkpoints.json" "$OUT" <<'PY'
import json, subprocess, sys

checkpoints_path, out = sys.argv[1], sys.argv[2]
with open(checkpoints_path) as fh:
    checkpoints = json.load(fh)

results = []
earned = 0.0
total = 0.0
passed = failed = 0
for cp in checkpoints:
    weight = float(cp.get("weight", 1.0))
    total += weight
    cmd = (cp.get("verify_cmd") or "").strip()
    if not cmd:
        # Fail closed: an empty command would make `bash -c ""` exit 0 and
        # silently "pass" a broken checkpoint, inflating reward.
        ok, code = False, None
    else:
        proc = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
        ok, code = proc.returncode == 0, proc.returncode
    earned += weight if ok else 0.0
    passed += 1 if ok else 0
    failed += 0 if ok else 1
    results.append({"id": cp["id"], "title": cp.get("title", cp["id"]),
                    "weight": weight, "passed": ok, "exit_code": code})

reward = round(earned / total, 6) if total else 0.0
with open(f"{out}/reward.txt", "w") as fh:
    fh.write(str(reward))
with open(f"{out}/metrics.json", "w") as fh:
    json.dump({"schema_version": 1, "reward": reward,
               "checkpoints_passed": passed, "checkpoints_total": len(checkpoints),
               "checkpoints": results}, fh, indent=2)
with open(f"{out}/ctrf.json", "w") as fh:
    json.dump({"results": {"tool": {"name": "crackbench-verifier"},
               "summary": {"tests": len(checkpoints), "passed": passed,
                           "failed": failed, "pending": 0, "skipped": 0, "other": 0},
               "tests": [{"name": r["id"],
                          "status": "passed" if r["passed"] else "failed"}
                         for r in results]}}, fh, indent=2)
print(f"crackbench reward={reward} ({passed}/{len(checkpoints)} checkpoints)")
PY
'''


def render_checkpoints_json(task: GeneratedTask) -> str:
    return json.dumps([cp.to_dict() for cp in task.checkpoints], indent=2)


def write_task_dir(
    task: GeneratedTask, dest: Path, *, long_horizon_minutes: float = 30.0
) -> Path:
    """Write the full Harbor task directory. Returns ``dest``."""
    dest = Path(dest)
    (dest / "environment").mkdir(parents=True, exist_ok=True)
    (dest / "solution").mkdir(parents=True, exist_ok=True)
    (dest / "tests").mkdir(parents=True, exist_ok=True)

    (dest / "task.toml").write_text(
        render_task_toml(task, long_horizon_minutes=long_horizon_minutes),
        encoding="utf-8",
    )
    (dest / "instruction.md").write_text(render_instruction(task), encoding="utf-8")
    (dest / "environment" / "Dockerfile").write_text(
        render_dockerfile(task), encoding="utf-8"
    )
    (dest / "solution" / "README.md").write_text(
        render_solution(task), encoding="utf-8"
    )
    (dest / "tests" / "checkpoints.json").write_text(
        render_checkpoints_json(task), encoding="utf-8"
    )
    test_sh = dest / "tests" / "test.sh"
    test_sh.write_text(_VERIFIER_TEMPLATE, encoding="utf-8")
    test_sh.chmod(0o755)
    return dest
