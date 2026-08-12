import { isAgentTrial, type Task, type Trial } from "@/lib/types";

const DEFAULT_EXPERIMENT_MODEL_KEY = "default";
const GEMINI_35_DISPLAY_AGENT = "gemini-cli";
const GEMINI_35_DISPLAY_MODEL = "gemini/gemini-3.5-flash";
const GEMINI_35_AGENT_ALIASES = new Set([
  "gemini-cli",
  "gemini-cli-api-key-no-search",
]);
const GEMINI_35_MODEL_ALIASES = new Set([
  "gemini/gemini-3.5-flash",
  "google/gemini-3.5-flash",
]);

export const PROBE_AGENT_KEY = "probe";

export type ExperimentAgentSummary = {
  key: string;
  label: string;
  agent: string;
  model: string | null;
  queueKey: string | null;
  isModelScoped: boolean;
};

// The "nop" baseline (no-op) makes no changes; the "oracle" baseline runs the
// known-good gold solution. Each may appear bare or with a suffix/prefix
// (`nop-foo`, `agent-oracle`), so match the family rather than the exact name.
export function isNopAgentName(name: string): boolean {
  const lower = name.toLowerCase();
  return (
    lower === "nop" || lower.startsWith("nop-") || lower.startsWith("agent-nop")
  );
}

export function isOracleAgentName(name: string): boolean {
  const lower = name.toLowerCase();
  return (
    lower === "oracle" ||
    lower.startsWith("oracle-") ||
    lower.startsWith("agent-oracle")
  );
}

// Baseline agents (nop / oracle) are deterministic validation runs, so they
// are excluded from score aggregation and row-filter evaluation.
export function isBaselineAgentName(name: string): boolean {
  return isNopAgentName(name) || isOracleAgentName(name);
}

function getModelKey(model: string | null | undefined): string {
  const trimmed = model?.trim();
  return trimmed && trimmed.length > 0 ? trimmed : DEFAULT_EXPERIMENT_MODEL_KEY;
}

function getDisplayAgentModel(
  trial: Pick<Trial, "agent" | "model">
): Pick<Trial, "agent" | "model"> {
  const agent = trial.agent.trim().toLowerCase();
  const model = trial.model?.trim().toLowerCase() ?? null;

  // These historical labels represent the same Gemini CLI + Flash 3.5
  // execution cohort. Canonicalize only the experiment display key; the
  // underlying trial metadata and provenance remain unchanged.
  if (
    GEMINI_35_AGENT_ALIASES.has(agent) &&
    model !== null &&
    GEMINI_35_MODEL_ALIASES.has(model)
  ) {
    return {
      agent: GEMINI_35_DISPLAY_AGENT,
      model: GEMINI_35_DISPLAY_MODEL,
    };
  }

  return trial;
}

function getModelScopedAgents(tasks: Task[]): Set<string> {
  const modelsByAgent = new Map<string, Set<string>>();

  for (const task of tasks) {
    for (const trial of task.trials ?? []) {
      if (trial.is_probe || !isAgentTrial(trial)) continue;
      const display = getDisplayAgentModel(trial);
      const existing = modelsByAgent.get(display.agent) ?? new Set<string>();
      existing.add(getModelKey(display.model));
      modelsByAgent.set(display.agent, existing);
    }
  }

  return new Set(
    Array.from(modelsByAgent.entries())
      .filter(([, models]) => models.size > 1)
      .map(([agent]) => agent)
  );
}

export function getExperimentAgentKey(
  trial: Pick<Trial, "agent" | "model" | "is_probe" | "kind">,
  modelScopedAgents: ReadonlySet<string>
): string {
  if (trial.is_probe) {
    return PROBE_AGENT_KEY;
  }
  // QA / audit trials are the platform's own runs, not the agent under
  // test: give them their own column so a qa-report experiment does not
  // mirror the agent matrix.
  if (!isAgentTrial(trial)) {
    return trial.kind as string;
  }
  const display = getDisplayAgentModel(trial);
  if (!modelScopedAgents.has(display.agent)) {
    return display.agent;
  }
  return `${display.agent}/${getModelKey(display.model)}`;
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
          label: "probe",
          agent: PROBE_AGENT_KEY,
          model: null,
          queueKey: null,
          isModelScoped: false,
        });
        continue;
      }

      if (!isAgentTrial(trial)) {
        summaries.set(key, {
          key,
          label: trial.kind === "qa" ? "QA run" : "Pre-trial audit",
          agent: trial.agent,
          model: trial.model ?? null,
          queueKey: trial.provider ?? null,
          isModelScoped: false,
        });
        continue;
      }

      const display = getDisplayAgentModel(trial);
      summaries.set(key, {
        key,
        label: key,
        agent: display.agent,
        model: display.model,
        queueKey: trial.provider ?? null,
        isModelScoped: modelScopedAgents.has(display.agent),
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
