"""Run a Harbor task and immediately classify the trial output."""

import asyncio
import os
from pathlib import Path

from oddish.workers.harbor_runner import run_harbor_trial_async
from oddish.analyze import TrialClassifier
from harbor.models.environment_type import EnvironmentType


async def run_and_analyze(task_path: Path, agent: str, model: str) -> None:
    jobs_dir = Path("jobs")
    jobs_dir.mkdir(exist_ok=True)

    print(f"Running trial: {task_path.name} with {agent}...")
    outcome = await run_harbor_trial_async(
        task_path=task_path,
        agent=agent,
        jobs_dir=jobs_dir,
        model=model,
        environment=EnvironmentType.DOCKER,
        harbor_config={"agent_config": {"env": {"ANTHROPIC_API_KEY": os.environ["ANTHROPIC_API_KEY"]}}},
    )

    print(f"Trial complete — reward={outcome.reward}, error={outcome.error}")

    if outcome.job_dir is None:
        print("No job dir produced, cannot classify.")
        return

    # Find the trial subdirectory (Harbor nests it one level down)
    trial_dirs = [d for d in outcome.job_dir.iterdir() if d.is_dir()]
    if not trial_dirs:
        print("No trial subdirectory found.")
        return
    trial_dir = trial_dirs[0]

    print(f"\nClassifying trial: {trial_dir.name}...")
    classifier = TrialClassifier(verbose=True)
    result = await classifier.classify_trial(
        trial_dir=trial_dir,
        task_dir=task_path,
    )

    print(f"\n--- Classification Result ---")
    print(f"Classification : {result.classification.value}")
    print(f"Subtype        : {result.subtype}")
    print(f"Evidence       : {result.evidence}")
    print(f"Root cause     : {result.root_cause}")
    print(f"Recommendation : {result.recommendation}")
    print(f"Reward         : {result.reward}")


if __name__ == "__main__":
    asyncio.run(run_and_analyze(
        task_path=Path("/Users/kateyeh/developer/os_repos/harbor/examples/tasks/hello-world"),
        agent="claude-code",
        model="anthropic/claude-sonnet-4-6",
    ))
