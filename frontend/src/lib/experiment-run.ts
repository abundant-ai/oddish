export interface ExperimentRunRequest {
  taskId: string;
  key: string;
  body: string;
}

export function buildExperimentRunRequests(
  taskIds: string[],
  experimentId: string,
  agent: string,
  model: string,
  efforts: string[],
  repetitions: number,
  operationId: string
): ExperimentRunRequest[] {
  if (
    !agent.trim() ||
    !model.trim() ||
    efforts.length === 0 ||
    !Number.isInteger(repetitions) ||
    repetitions < 1 ||
    repetitions > 100
  ) {
    throw new Error(
      "Choose an agent, model, effort, and 1–100 trials per effort."
    );
  }
  return [...new Set(taskIds)].map((taskId) => ({
    taskId,
    key: `${operationId}:${taskId}`,
    body: JSON.stringify({
      task_id: taskId,
      experiment_id: experimentId,
      append_to_task: true,
      add_trials: true,
      configs: [...new Set(efforts)].map((effort) => ({
        agent: agent.trim(),
        model: model.trim(),
        n_trials: repetitions,
        ...(effort === "default"
          ? {}
          : { agent_config: { kwargs: { reasoning_effort: effort } } }),
      })),
    }),
  }));
}

/** Bound in-flight writes; retain each original body/key for safe transport retries. */
export async function submitExperimentRuns(
  requests: ExperimentRunRequest[],
  send: (url: string, init: RequestInit) => Promise<Response>,
  onProgress: (completed: number) => void
): Promise<{ failed: ExperimentRunRequest[]; errors: string[] }> {
  const failed: ExperimentRunRequest[] = [];
  const errors: string[] = [];
  let completed = 0;
  for (let offset = 0; offset < requests.length; offset += 4) {
    await Promise.all(
      requests.slice(offset, offset + 4).map(async (request) => {
        try {
          const response = await send("/api/tasks/sweep", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "Idempotency-Key": request.key,
            },
            body: request.body,
          });
          if (!response.ok) {
            const error = await response.json().catch(() => null);
            throw new Error(
              error?.detail || error?.error || `HTTP ${response.status}`
            );
          }
        } catch (error) {
          failed.push(request);
          errors.push(
            `${request.taskId}: ${error instanceof Error ? error.message : "Submission failed"}`
          );
        } finally {
          onProgress(++completed);
        }
      })
    );
  }
  return { failed, errors };
}
