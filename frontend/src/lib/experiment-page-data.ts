import type {
  ExperimentOpenResponse,
  ExperimentTrialCell,
  ExperimentTrialPageResponse,
  PublicExperimentOpenResponse,
  Task,
  Trial,
} from "./types";

type OpenResponse = ExperimentOpenResponse | PublicExperimentOpenResponse;

export function trialFromExperimentCell(cell: ExperimentTrialCell): Trial {
  const { analysis, ...trial } = cell;
  return {
    ...trial,
    analysis_status: analysis.status,
    analysis_started_at: analysis.started_at,
    analysis_finished_at: analysis.finished_at,
    analysis: analysis.classification
      ? {
          classification: analysis.classification,
          subtype: analysis.subtype ?? undefined,
          evidence: analysis.evidence ?? undefined,
        }
      : null,
  };
}

/**
 * Join independently cached task and trial pages without crossing the task
 * version selected by the experiment. A stale page may remain cached after a
 * version pivot or a failed refresh, so task id alone is not sufficient.
 */
export function buildExperimentTasks(
  openPages: OpenResponse[] | undefined,
  trialPages: ExperimentTrialPageResponse[] | undefined,
  publicView: boolean
): Task[] {
  const experiment = openPages?.[0];
  if (!experiment) return [];

  const cellsByTask = new Map<string, ExperimentTrialCell[]>();
  for (const page of trialPages ?? []) {
    for (const cell of page.trials) {
      const cells = cellsByTask.get(cell.task_id) ?? [];
      cells.push(cell);
      cellsByTask.set(cell.task_id, cells);
    }
  }

  return openPages.flatMap((page) =>
    page.tasks.map((task) => {
      const identity =
        !publicView && "owner" in experiment
          ? {
              experiment_owner: experiment.owner,
              experiment_link: experiment.link,
            }
          : {};
      const taskCells = cellsByTask.get(task.id);
      const trials = taskCells?.filter(
        (cell) =>
          (cell.task_version_id ?? null) === (task.trial_version_id ?? null)
      );
      return {
        ...task,
        experiment_id: experiment.experiment_id,
        experiment_name: experiment.name,
        experiment_is_public: publicView,
        experiment_created_at: experiment.created_at,
        ...identity,
        trials: trials?.map(trialFromExperimentCell),
      };
    })
  );
}
