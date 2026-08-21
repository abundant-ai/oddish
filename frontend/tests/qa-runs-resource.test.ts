import assert from "node:assert/strict";
import test from "node:test";

import {
  qaRunsKey,
  qaRunsRefreshInterval,
} from "../src/lib/qa-runs-resource.ts";
import type { QaRun } from "../src/lib/types.ts";

function run(status: QaRun["status"]): QaRun {
  return { status } as QaRun;
}

test("waits for the task version before requesting QA history", () => {
  assert.equal(qaRunsKey("/api", "task-1", undefined), null);
  assert.equal(qaRunsKey("/api", null, 3), null);
});

test("scopes QA history to a selected version or all versions explicitly", () => {
  assert.equal(
    qaRunsKey("/api", "task-1", 3),
    "/api/tasks/task-1/qa/runs?version=3"
  );
  assert.equal(qaRunsKey("/api", "task-1", null), "/api/tasks/task-1/qa/runs");
});

test("polls only while at least one QA run is active", () => {
  assert.equal(qaRunsRefreshInterval([run("success"), run("failed")]), 0);
  assert.equal(qaRunsRefreshInterval([run("success"), run("running")]), 5000);
  assert.equal(qaRunsRefreshInterval(undefined), 0);
});
