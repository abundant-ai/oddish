import { expect, test } from "@playwright/test";

import {
  buildExperimentAgentSummaries,
  getExperimentAgentKey,
} from "../src/lib/experiment-agent-grouping";
import type { Task, Trial } from "../src/lib/types";

function trial(
  agent: string,
  model: string,
  overrides: Partial<Trial> = {}
): Trial {
  return {
    id: `${agent}-${model}`,
    name: `${agent}-${model}`,
    task_id: "task-1",
    task_path: "tasks/task-1",
    agent,
    provider: "gemini",
    model,
    status: "success",
    attempts: 1,
    max_attempts: 1,
    harbor_stage: "completed",
    reward: 0,
    created_at: "2026-07-29T00:00:00Z",
    ...overrides,
  };
}

function task(trials: Trial[]): Task {
  return {
    id: "task-1",
    name: "Task one",
    status: "completed",
    priority: "low",
    user: "tester",
    task_path: "tasks/task-1",
    experiment_id: "exp-1",
    experiment_name: "Experiment one",
    experiment_is_public: false,
    total: trials.length,
    completed: trials.length,
    failed: 0,
    trials,
    created_at: "2026-07-29T00:00:00Z",
    updated_at: "2026-07-29T00:00:00Z",
  };
}

test("groups Gemini 3.5 Flash label and harness aliases into one column", () => {
  const aliases = [
    trial("gemini-cli", "gemini/gemini-3.5-flash"),
    trial("gemini-cli", "google/gemini-3.5-flash"),
    trial("gemini-cli-api-key-no-search", "google/gemini-3.5-flash"),
    trial("gemini-cli", "gemini/gemini-3.6-flash"),
  ];
  const { agentSummaries, modelScopedAgents } = buildExperimentAgentSummaries([
    task(aliases),
  ]);

  expect(agentSummaries.map((summary) => summary.key)).toEqual([
    "gemini-cli/gemini/gemini-3.5-flash",
    "gemini-cli/gemini/gemini-3.6-flash",
  ]);
  expect(
    aliases
      .slice(0, 3)
      .map((item) => getExperimentAgentKey(item, modelScopedAgents))
  ).toEqual([
    "gemini-cli/gemini/gemini-3.5-flash",
    "gemini-cli/gemini/gemini-3.5-flash",
    "gemini-cli/gemini/gemini-3.5-flash",
  ]);
});

test("does not relabel other Gemini models or non-Gemini harnesses", () => {
  const unrelated = [
    trial("gemini-cli-api-key-no-search", "google/gemini-3.6-flash"),
    trial("mini-swe-agent", "google/gemini-3.5-flash"),
  ];
  const { agentSummaries } = buildExperimentAgentSummaries([task(unrelated)]);

  expect(agentSummaries.map((summary) => summary.key)).toEqual([
    "gemini-cli-api-key-no-search",
    "mini-swe-agent",
  ]);
});
