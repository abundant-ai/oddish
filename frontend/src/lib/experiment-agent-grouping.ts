import type { Task, Trial } from "@/lib/types";

export const DEFAULT_EXPERIMENT_MODEL_KEY = "default";

export type ExperimentAgentSummary = {
  key: string;
  label: string;
  agent: string;
  model: string | null;
  queueKey: string | null;
  isModelScoped: boolean;
};

function getModelKey(model: string | null | undefined): string {
  const trimmed = model?.trim();
  return trimmed && trimmed.length > 0 ? trimmed : DEFAULT_EXPERIMENT_MODEL_KEY;
}

export function getModelScopedAgents(tasks: Task[]): Set<string> {
  const modelsByAgent = new Map<string, Set<string>>();

  for (const task of tasks) {
    for (const trial of task.trials ?? []) {
      const existing = modelsByAgent.get(trial.agent) ?? new Set<string>();
      existing.add(getModelKey(trial.model));
      modelsByAgent.set(trial.agent, existing);
    }
  }

  return new Set(
    Array.from(modelsByAgent.entries())
      .filter(([, models]) => models.size > 1)
      .map(([agent]) => agent)
  );
}

export function getExperimentAgentKey(
  trial: Pick<Trial, "agent" | "model">,
  modelScopedAgents: ReadonlySet<string>
): string {
  if (!modelScopedAgents.has(trial.agent)) {
    return trial.agent;
  }
  return `${trial.agent}@${getModelKey(trial.model)}`;
}

export function buildExperimentAgentSummaries(tasks: Task[]): {
  agentSummaries: ExperimentAgentSummary[];
  modelScopedAgents: Set<string>;
} {
  const modelScopedAgents = getModelScopedAgents(tasks);
  const summaries = new Map<string, ExperimentAgentSummary>();

  for (const task of tasks) {
    for (const trial of task.trials ?? []) {
      const isModelScoped = modelScopedAgents.has(trial.agent);
      const key = getExperimentAgentKey(trial, modelScopedAgents);
      if (summaries.has(key)) continue;

      summaries.set(key, {
        key,
        label: key,
        agent: trial.agent,
        model: trial.model,
        queueKey: trial.provider ?? null,
        isModelScoped,
      });
    }
  }

  return {
    agentSummaries: Array.from(summaries.values()),
    modelScopedAgents,
  };
}
