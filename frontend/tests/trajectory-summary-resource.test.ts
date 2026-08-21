import assert from "node:assert/strict";
import test from "node:test";

import { parseTrajectorySummaryResponse } from "../src/lib/use-trajectory-summary.ts";

const response = (status: number, statusText = "") => ({
  ok: status >= 200 && status < 300,
  status,
  statusText,
});

test("parses a published summary", () => {
  const summary = { schema_version: 5, summary: "done", components: [] };
  assert.deepEqual(parseTrajectorySummaryResponse(response(200), summary), {
    status: "ready",
    summary,
  });
});

test("maps 404 to the renderable missing state", () => {
  assert.deepEqual(
    parseTrajectorySummaryResponse(response(404, "Not Found"), {
      detail: "No trajectory summary",
    }),
    { status: "missing" }
  );
});

for (const status of ["queued", "running", "retrying", "settling"] as const) {
  test(`parses the ${status} lifecycle state`, () => {
    assert.deepEqual(
      parseTrajectorySummaryResponse(response(202), {
        status,
        job_id: "task-1-9",
        retry_after_ms: 3000,
      }),
      {
        status,
        summary: null,
        jobId: "task-1-9",
        retryAfterMs: 3000,
      }
    );
  });
}

test("preserves a failed refresh HTTP response", () => {
  assert.throws(
    () =>
      parseTrajectorySummaryResponse(response(409, "Conflict"), {
        detail: "Trajectory summary refresh failed",
      }),
    (error: Error & { status?: number }) =>
      error.message === "Trajectory summary refresh failed" &&
      error.status === 409
  );
});

test("rejects an unknown pending status instead of assuming queued", () => {
  assert.throws(
    () =>
      parseTrajectorySummaryResponse(response(202), {
        status: "blocked",
        job_id: "task-1-9",
        retry_after_ms: 3000,
      }),
    /Malformed trajectory summary pending response/
  );
});

test("rejects a missing job id", () => {
  assert.throws(
    () =>
      parseTrajectorySummaryResponse(response(202), {
        status: "queued",
        retry_after_ms: 3000,
      }),
    /Malformed trajectory summary pending response/
  );
});

test("rejects a non-positive retry interval", () => {
  assert.throws(
    () =>
      parseTrajectorySummaryResponse(response(202), {
        status: "queued",
        job_id: "task-1-9",
        retry_after_ms: 0,
      }),
    /Malformed trajectory summary pending response/
  );
});
