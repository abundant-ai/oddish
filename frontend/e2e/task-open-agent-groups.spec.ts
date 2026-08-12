import { expect, test } from "@playwright/test";

import { buildTaskOpenAgentGroups } from "../src/lib/task-open-agent-groups";
import type { TaskOpenAgentModelSummary, Trial } from "../src/lib/types";

function summary(
  agent: string,
  model: string,
  overrides: Partial<TaskOpenAgentModelSummary> = {}
): TaskOpenAgentModelSummary {
  return {
    agent,
    model,
    providers: ["gemini"],
    is_probe: false,
    trial_count: 1,
    completed_count: 1,
    failed_count: 0,
    skipped_count: 0,
    pending_count: 0,
    pass_count: 1,
    partial_count: 0,
    fail_count: 0,
    reward_sum: 1,
    reward_total: 1,
    cost_usd: 1,
    cost_trial_count: 1,
    cost_has_estimated: false,
    cost_has_native: true,
    billed_cost_usd: 1,
    billed_trial_count: 1,
    billed_has_estimated: false,
    billed_has_native: true,
    last_run_at: "2026-08-11T00:00:00Z",
    duration_sum_seconds: 30,
    duration_trial_count: 1,
    ...overrides,
  };
}

function trial(
  id: string,
  agent: string,
  model: string,
  overrides: Partial<Trial> = {}
): Trial {
  return {
    id,
    name: id,
    task_id: "task-1",
    task_path: "tasks/task-1",
    agent,
    provider: "gemini",
    model,
    status: "success",
    attempts: 1,
    max_attempts: 1,
    harbor_stage: "completed",
    reward: 1,
    created_at: "2026-08-11T00:00:00Z",
    ...overrides,
  };
}

test("coalesces display aliases into one exact task-open card", () => {
  const summaries = [
    summary("gemini-cli", "gemini/gemini-3.5-flash", {
      providers: ["gemini"],
      trial_count: 2,
      cost_usd: 2,
      last_run_at: "2026-08-11T00:00:00Z",
    }),
    summary("gemini-cli-api-key-no-search", "google/gemini-3.5-flash", {
      providers: ["google", "gemini"],
      trial_count: 3,
      cost_usd: 3,
      last_run_at: "2026-08-12T00:00:00Z",
    }),
  ];
  const previews = [
    trial("trial-a", "gemini-cli", "gemini/gemini-3.5-flash"),
    trial("trial-b", "gemini-cli-api-key-no-search", "google/gemini-3.5-flash"),
  ];

  const result = buildTaskOpenAgentGroups(summaries, previews);

  expect(result.realAgentCount).toBe(1);
  expect(result.realTrialCount).toBe(5);
  expect(result.agentCards).toHaveLength(1);
  expect(result.agentCards[0]).toMatchObject({
    key: "gemini-cli",
    summary: {
      agent: "gemini-cli",
      model: "gemini/gemini-3.5-flash",
      providers: ["gemini", "google"],
      trial_count: 5,
      cost_usd: 5,
      last_run_at: "2026-08-12T00:00:00Z",
    },
  });
  expect(result.agentCards[0].trials.map((item) => item.id)).toEqual([
    "trial-a",
    "trial-b",
  ]);
});

test("keeps genuinely distinct models in separate model-scoped cards", () => {
  const result = buildTaskOpenAgentGroups(
    [summary("codex", "openai/gpt-5.5"), summary("codex", "openai/gpt-5.6")],
    []
  );

  expect(result.agentCards.map((card) => card.key)).toEqual([
    "codex/openai/gpt-5.5",
    "codex/openai/gpt-5.6",
  ]);
});
