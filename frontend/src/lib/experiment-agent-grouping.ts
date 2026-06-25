import type { Task, Trial } from "@/lib/types";

const DEFAULT_EXPERIMENT_MODEL_KEY = "default";

export const PROBE_AGENT_KEY = "Probe";

export type ExperimentAgentSummary = {
  key: string;
  label: string;
  agent: string;
  model: string | null;
  queueKey: string | null;
  isModelScoped: boolean;
};

// Baseline agents (nop / oracle) are deterministic validation runs, so they
// are excluded from score aggregation and row-filter evaluation.
export function isBaselineAgentName(name: string): boolean {
  const lower = name.toLowerCase();
  return (
    lower === "nop" ||
    lower === "oracle" ||
    lower.startsWith("nop-") ||
    lower.startsWith("oracle-") ||
    lower.startsWith("agent-nop") ||
    lower.startsWith("agent-oracle")
  );
}

function getModelKey(model: string | null | undefined): string {
  const trimmed = model?.trim();
  return trimmed && trimmed.length > 0 ? trimmed : DEFAULT_EXPERIMENT_MODEL_KEY;
}

function getModelScopedAgents(tasks: Task[]): Set<string> {
  const modelsByAgent = new Map<string, Set<string>>();

  for (const task of tasks) {
    for (const trial of task.trials ?? []) {
      if (trial.is_probe) continue;
      const existing = modelsByAgent.get(trial.agent) ?? new Set<string>();
      existing.add(getModelKey(trial.model));
      modelsByAgent.set(trial.agent, existing);
    }
  }

  return new Set(
    Array.from(modelsByAgent.entries())
      .filter(([, models]) => models.size > 1)
      .map(([agent]) => agent),
  );
}

export function getExperimentAgentKey(
  trial: Pick<Trial, "agent" | "model" | "is_probe">,
  modelScopedAgents: ReadonlySet<string>,
): string {
  if (trial.is_probe) {
    return PROBE_AGENT_KEY;
  }
  if (!modelScopedAgents.has(trial.agent)) {
    return trial.agent;
  }
  return `${trial.agent}/${getModelKey(trial.model)}`;
}

export function buildExperimentAgentSummaries(tasks: Task[]): {
  agentSummaries: ExperimentAgentSummary[];
  modelScopedAgents: Set<string>;
} {
  const modelScopedAgents = getModelScopedAgents(tasks);
  const summaries = new Map<string, ExperimentAgentSummary>();

  for (const task of tasks) {
    for (const trial of task.trials ?? []) {
      const key = getExperimentAgentKey(trial, modelScopedAgents);
      if (summaries.has(key)) continue;

      if (trial.is_probe) {
        summaries.set(key, {
          key: PROBE_AGENT_KEY,
          label: "Probe",
          agent: PROBE_AGENT_KEY,
          model: null,
          queueKey: null,
          isModelScoped: false,
        });
        continue;
      }

      summaries.set(key, {
        key,
        label: key,
        agent: trial.agent,
        model: trial.model,
        queueKey: trial.provider ?? null,
        isModelScoped: modelScopedAgents.has(trial.agent),
      });
    }
  }

  const ordered = Array.from(summaries.values());
  const probeIndex = ordered.findIndex((s) => s.key === PROBE_AGENT_KEY);
  if (probeIndex >= 0) {
    ordered.push(ordered.splice(probeIndex, 1)[0]);
  }

  return { agentSummaries: ordered, modelScopedAgents };
}
