import assert from "node:assert/strict";
import test from "node:test";
import { buildUsageRows } from "../src/lib/usage-rows.ts";
import type { JobUsage, ModelUsage, QueueStats } from "../src/lib/types.ts";

const jobs = [
  {
    kind: "TRIAL",
    queue_key: "nop_oracle",
    job_count: 20,
    running: 4,
    queued: 2,
    retrying: 15,
    avg_duration_s: null,
  },
] as JobUsage[];
const historical = {
  "old/gemini": { retrying: 135, running: 1, success: 400 },
  "old/xai": { retrying: 129, running: 3, success: 200 },
  "old/other": { retrying: 102, running: 76, success: 100 },
} as QueueStats;

test("historical trial statuses do not inflate the live usage badges", () => {
  // Reproduces the production discrepancy: 15 live retries plus 366 old mirrors.
  const rows = buildUsageRows(jobs, [], historical);
  assert.deepEqual(
    ["running", "queued", "retrying"].map((key) =>
      rows.reduce((total, row) => total + row[key as "running"], 0)
    ),
    [4, 2, 15]
  );
});

test("model spending stays visible without claiming historical runs are active", () => {
  const usage = [
    {
      model: "old/gemini",
      provider: "google",
      trial_count: 4,
      cost_usd: 12,
      input_tokens: 100,
      output_tokens: 10,
      cache_tokens: 0,
      running: 1,
      queued: 2,
      retrying: 135,
      avg_duration_s: 90,
    },
  ] as ModelUsage[];
  const row = buildUsageRows([], usage, historical).find(
    (r) => r.key === "old/gemini"
  )!;
  assert.equal(row.costUsd, 12);
  assert.deepEqual([row.running, row.queued, row.retrying], [0, 0, 0]);
});

test("a worker older than the spending window remains in live counts", () => {
  const oldJob = {
    ...jobs[0],
    job_count: 0,
    running: 1,
    queued: 0,
    retrying: 0,
  };
  const [row] = buildUsageRows([oldJob], [], null);
  assert.equal(row.running, 1);
  assert.equal(row.jobCount, 0);
});
