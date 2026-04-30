"""Run TrialClassifier on all trial directories found under jobs/."""

import asyncio
import json
from pathlib import Path

from oddish.analyze import TrialClassifier


def find_trials(jobs_dir: Path) -> list[tuple[Path, Path]]:
    """Return (trial_dir, task_dir) pairs for every trial under jobs_dir."""
    trials = []
    for result_json in jobs_dir.rglob("result.json"):
        trial_dir = result_json.parent
        # Skip job-level result.json (they have a 'stats' key, not 'task_id')
        try:
            data = json.loads(result_json.read_text())
        except Exception:
            continue
        task_id = data.get("task_id")
        if not isinstance(task_id, dict) or "path" not in task_id:
            continue
        task_dir = Path(task_id["path"])
        if not task_dir.exists():
            print(f"  [skip] task dir not found: {task_dir}")
            continue
        trials.append((trial_dir, task_dir))
    return trials


async def classify_all(jobs_dir: Path) -> None:
    trials = find_trials(jobs_dir)
    if not trials:
        print("No trials found.")
        return

    print(f"Found {len(trials)} trial(s)\n")
    classifier = TrialClassifier(verbose=True)

    for trial_dir, task_dir in trials:
        print(f"{'='*60}")
        print(f"Trial : {trial_dir.name}")
        print(f"Task  : {task_dir.name}")
        result = await classifier.classify_trial(
            trial_dir=trial_dir,
            task_dir=task_dir,
        )
        print(f"Classification : {result.classification.value}")
        print(f"Subtype        : {result.subtype}")
        print(f"Evidence       : {result.evidence}")
        print(f"Root cause     : {result.root_cause}")
        print(f"Recommendation : {result.recommendation}")
        print(f"Reward         : {result.reward}")
        print()


if __name__ == "__main__":
    asyncio.run(classify_all(Path("jobs")))
