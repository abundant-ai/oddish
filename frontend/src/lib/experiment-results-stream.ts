import type {
  ExperimentOpenResponse,
  PublicExperimentOpenResponse,
  ExperimentTrialCell,
} from "./types";

export type ExperimentResults = {
  experiment: ExperimentOpenResponse | PublicExperimentOpenResponse;
  trials: ExperimentTrialCell[];
};
type ResultRecord =
  | { type: "experiment"; experiment: ExperimentResults["experiment"] }
  | { type: "task"; task: ExperimentResults["experiment"]["tasks"][number] }
  | { type: "trial"; trial: ExperimentTrialCell }
  | { type: "complete" };

/** Read records across arbitrary network chunk boundaries; EOF alone is not success. */
export async function readExperimentResults(
  response: Response,
  onProgress: (results: ExperimentResults) => void
): Promise<ExperimentResults> {
  if (!response.ok) {
    const error = await response.json().catch(() => null);
    throw new Error(
      error?.detail ||
        error?.error ||
        `Results request failed (${response.status})`
    );
  }
  if (!response.body) throw new Error("Results response has no body");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let pending = "";
  let results: ExperimentResults | undefined;
  let complete = false;
  function record(line: string) {
    if (!line.trim()) return;
    if (complete) throw new Error("Unexpected data after results completed");
    const event = JSON.parse(line) as ResultRecord;
    if (event.type === "experiment") {
      if (results) throw new Error("Duplicate experiment header");
      results = { experiment: event.experiment, trials: [] };
    } else {
      if (!results) throw new Error("Missing experiment header");
      switch (event.type) {
        case "task":
          results.experiment.tasks.push(event.task);
          break;
        case "trial":
          results.trials.push(event.trial);
          break;
        case "complete":
          complete = true;
          break;
        default:
          throw new Error("Unknown results record");
      }
    }
    onProgress(results);
  }
  try {
    while (true) {
      const { done, value } = await reader.read();
      pending += decoder.decode(value, { stream: !done });
      let newline: number;
      while ((newline = pending.indexOf("\n")) >= 0) {
        record(pending.slice(0, newline));
        pending = pending.slice(newline + 1);
      }
      if (done) break;
    }
    if (pending.trim()) record(pending);
    if (!complete || !results)
      throw new Error(
        "Results download was interrupted. Retry to load all results."
      );
    const summary = results.experiment.summary;
    if (
      summary &&
      (results.experiment.tasks.length !== summary.task_count ||
        results.trials.length !== summary.trial_count)
    ) {
      throw new Error(
        "Results download ended before all tasks and trials arrived."
      );
    }
    return results;
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
