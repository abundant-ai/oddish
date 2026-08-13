import { expect, test } from "@playwright/test";

import {
  taskOpenFromBrowse,
  taskOpenValue,
} from "../src/lib/task-open-resource";
import type { TaskBrowseItem } from "../src/lib/types";

const browse: TaskBrowseItem = {
  id: "task-1",
  name: "Exact browse snapshot",
  current_version: 3,
  current_version_id: "task-1-v3",
  version_count: 3,
  total_trials: 100,
  completed_trials: 70,
  failed_trials: 5,
  reward_success: 40,
  reward_sum: 45,
  reward_total: 70,
  pass_count: 40,
  partial_count: 10,
  fail_count: 20,
  harness_count: 0,
  skipped_count: 15,
  pending_count: 10,
  last_run_at: "2026-08-11T00:00:00Z",
  cost_usd: 10,
  cost_trial_count: 70,
  cost_has_estimated: false,
  cost_has_native: true,
  billed_cost_usd: 8,
  billed_trial_count: 60,
  billed_has_estimated: false,
  billed_has_native: true,
  latest_trials: [
    {
      id: "trial-100",
      name: "Only preview row",
      status: "success",
      reward: 1,
      error_message: null,
      agent: "codex",
      model: "openai/gpt-5.6",
    },
  ],
  latest_trials_truncated: true,
  experiments: [],
  user_tags: [
    {
      tag_id: "task-effective-tag",
      key: "effective",
      visibility: "PRIVATE",
      current: true,
      older: false,
    },
  ],
};

test("browse snapshots keep exact counters independent of the trial preview", () => {
  const open = taskOpenValue(taskOpenFromBrowse(browse));

  expect(open?.task.status).toBe("running");
  expect(open?.selected_version).toMatchObject({
    trial_count: 100,
    completed_count: 70,
    failed_count: 5,
    skipped_count: 15,
    pending_count: 10,
    pass_count: 40,
    partial_count: 10,
    fail_count: 20,
  });
  expect(open?.trials).toHaveLength(1);
  expect(open?.trials_has_more).toBe(true);
});

test("browse snapshots do not mislabel effective task tags as direct version tags", () => {
  const open = taskOpenValue(taskOpenFromBrowse(browse));

  expect(open?.task.user_tags).toHaveLength(1);
  expect(open?.selected_version?.user_tags).toEqual([]);
});
