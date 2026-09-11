import assert from "node:assert/strict";
import test from "node:test";
import {
  deliveryTaskState,
  deliveryOwnerTasks,
  deliveryProgressHistory,
} from "../src/lib/deliveries.ts";
import { board, taskRow, reviewTaskRow } from "../e2e/delivery-fixtures.ts";

test("retained findings take precedence over incomplete QA and a recorded sign-off", () => {
  const row = reviewTaskRow();
  assert.equal(deliveryTaskState(row), "needs_work");
  row.defects.forEach((finding) => (finding.acknowledged = true));
  row.checks.find((check) => check.key === "no_must_fix")!.status = "pass";
  row.ready = true;
  assert.equal(deliveryTaskState(row), "ready");
  assert.equal(row.qa.status, "error"); // Explicit exceptions satisfy requirements; QA did not pass.
});

test("QA acceptance is not sign-off, and elapsed days do not change readiness", () => {
  const row = taskRow();
  row.qa = {
    status: "accepted",
    finished_at: "2020-01-01T00:00:00Z",
    detail: "Current version reviewed",
  };
  row.checks[0].status = "pass";
  assert.equal(deliveryTaskState(row), "awaiting_signoff");
  row.checks[1].status = "pass";
  row.ready = true;
  assert.equal(deliveryTaskState(row), "ready");
  row.checks[0].status = "fail";
  row.ready = false;
  assert.equal(deliveryTaskState(row), "qa_incomplete");
});

for (const status of [
  "never",
  "queued",
  "running",
  "error",
  "outdated",
] as const) {
  test(`${status} review with unsatisfied checks remains QA incomplete`, () => {
    const row = taskRow();
    row.qa.status = status;
    row.checks[1].status = "pass";
    assert.equal(deliveryTaskState(row), "qa_incomplete");
  });
}

test("rejected verdict and missing task require work; a failed review alone is not a defect", () => {
  const row = taskRow();
  row.checks[0].key = "verdict_ok";
  row.qa.status = "needs_fixes";
  assert.equal(deliveryTaskState(row), "needs_work");
  row.qa.status = "error";
  assert.equal(deliveryTaskState(row), "qa_incomplete");
  row.checks[0].key = "task_exists";
  assert.equal(deliveryTaskState(row), "needs_work");
});

test("owner scopes include ready and unassigned work without treating signed-out as unassigned", () => {
  const data = board();
  data.tasks.push({ ...taskRow(), task_id: "ready", ready: true });
  data.tasks.push({
    ...taskRow(),
    task_id: "unassigned",
    qa_work: { ...taskRow().qa_work, owner_user_id: null },
  });
  assert.equal(deliveryOwnerTasks(data, "all").length, 3);
  assert.equal(deliveryOwnerTasks(data, "mine").length, 2);
  assert.equal(deliveryOwnerTasks(data, "maya").length, 2);
  assert.equal(deliveryOwnerTasks(data, "unassigned").length, 1);
  data.qa_viewer_user_id = null;
  assert.equal(deliveryOwnerTasks(data, "mine").length, 0);
});

test("owner history preserves missing observations and zero counts after reassignment", () => {
  const data = board();
  const counts = {
    task_count: 2,
    ready: 1,
    blocked: 1,
    awaiting_signoff: 0,
    unassigned: 0,
    open_findings: 0,
    acknowledged_findings: 0,
  };
  data.progress_history = [
    { ...counts, recorded_at: "2026-09-05T12:00:00Z" },
    {
      ...counts,
      recorded_at: "2026-09-06T12:00:00Z",
      owners: { maya: { task_count: 2, ready: 1 } },
    },
    {
      ...counts,
      recorded_at: "2026-09-08T12:00:00Z",
      owners: { jules: { task_count: 2, ready: 1 } },
    },
  ];
  assert.deepEqual(deliveryProgressHistory(data, "mine"), [
    { date: "2026-09-06", task_count: 2, ready: 1 },
    { date: "2026-09-07", task_count: null, ready: null },
    { date: "2026-09-08", task_count: 0, ready: 0 },
  ]);
  assert.equal(deliveryProgressHistory(data, "all")[0].date, "2026-09-05");
  data.progress_history = [data.progress_history[0]];
  assert.deepEqual(deliveryProgressHistory(data, "maya"), []);
});

test("rejected reviews require both finding acknowledgments and a verdict waiver", () => {
  const row = reviewTaskRow();
  row.qa.status = "needs_fixes";
  row.checks.find((check) => check.key === "verdict_ok")!.status = "fail";
  row.defects.forEach((finding) => (finding.acknowledged = true));
  row.checks.find((check) => check.key === "no_must_fix")!.status = "pass";
  assert.equal(deliveryTaskState(row), "needs_work");
  row.checks.find((check) => check.key === "verdict_ok")!.status = "waived";
  row.ready = false;
  assert.equal(deliveryTaskState(row), "awaiting_signoff");
  row.ready = true;
  assert.equal(deliveryTaskState(row), "ready");
});
