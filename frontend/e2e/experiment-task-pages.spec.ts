import { expect, test } from "@playwright/test";

import {
  fetchFreshExperimentTaskPage,
  mergeExperimentTaskPages,
} from "../src/lib/experiment-task-pages";
import type { Task, Trial } from "../src/lib/types";

function task(overrides: Partial<Task>): Task {
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
    total: 0,
    completed: 0,
    failed: 0,
    created_at: "2026-07-14T00:00:00Z",
    trials: null,
    ...overrides,
  };
}

test("keeps the fresh shell while a cached trial page describes another version", () => {
  const shell = task({ current_version: 1, current_version_id: "task-1-v1" });
  const staleEnriched = task({
    current_version: 2,
    current_version_id: "task-1-v2",
    total: 1,
    completed: 1,
    trials: [{ id: "trial-v2" } as Trial],
  });

  const [merged] = mergeExperimentTaskPages([shell], [[staleEnriched]]);

  expect(merged).toBe(shell);
  expect(merged.current_version_id).toBe("task-1-v1");
  expect(merged.trials).toBeNull();
});

test("adopts trial data once both loading phases agree on the version", () => {
  const shell = task({ current_version: 1, current_version_id: "task-1-v1" });
  const enriched = task({
    current_version: 1,
    current_version_id: "task-1-v1",
    total: 1,
    completed: 1,
    trials: [{ id: "trial-v1" } as Trial],
  });

  const [merged] = mergeExperimentTaskPages([shell], [[enriched]]);

  expect(merged).toBe(enriched);
  expect(merged.trials?.map((trial) => trial.id)).toEqual(["trial-v1"]);
});

test("revalidation bypasses stale browser-cache entries", async () => {
  let requestInput: RequestInfo | URL | undefined;
  let requestInit: RequestInit | undefined;
  const request = async (input: RequestInfo | URL, init?: RequestInit) => {
    requestInput = input;
    requestInit = init;
    return new Response("[]", {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };

  await fetchFreshExperimentTaskPage(
    "/api/experiments/exp-1/task-shells",
    request
  );

  expect(requestInput).toBe("/api/experiments/exp-1/task-shells");
  expect(requestInit).toEqual({ credentials: "include", cache: "no-store" });
});
