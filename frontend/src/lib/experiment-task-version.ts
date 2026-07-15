import type { Task } from "@/lib/types";

/**
 * Return the version represented by an experiment task's already-scoped trials.
 * Fall back to the selected task default when the experiment has no versioned
 * trial rows to anchor its file and reward views.
 */
export function resolveExperimentTaskVersion(task: Task): number | null {
  if (task.trial_version !== undefined) return task.trial_version;
  const representedVersion = task.trials?.find(
    (trial) => trial.task_version != null,
  )?.task_version;
  return representedVersion ?? task.current_version ?? null;
}
