import type { Task } from "@/lib/types";

/**
 * Prepare the already-scoped public experiment payload for display.
 *
 * The backend selects the experiment-relevant trial version while
 * `current_version` identifies the task's global selected default. Do not
 * re-filter trials against that display field here: historical and gathered
 * experiment runs can legitimately belong to another version.
 */
export function preparePublicExperimentTasks(data: Task[] | undefined): Task[] {
  const tasks = Array.isArray(data) ? [...data] : [];
  return tasks.sort(
    (a, b) =>
      new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
  );
}
