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
    summary,
    refresh: null,
  });
});

test("maps 404 to the renderable missing state", () => {
  assert.deepEqual(
    parseTrajectorySummaryResponse(response(404, "Not Found"), {
      detail: "No trajectory summary",
    }),
    { summary: null, refresh: null }
  );
});

for (const status of ["queued", "running", "retrying", "settling"] as const) {
  test(`parses the ${status} lifecycle state`, () => {
    assert.deepEqual(
      parseTrajectorySummaryResponse(response(202), {
        summary: null,
        refresh: {
          status,
          job_id: "task-1-9",
          retry_after_ms: 3000,
        },
      }),
      {
        summary: null,
        refresh: {
          status,
          jobId: "task-1-9",
          retryAfterMs: 3000,
        },
      }
    );
  });
}

test("parses the legacy top-level pending lifecycle during rolling deploys", () => {
  assert.deepEqual(
    parseTrajectorySummaryResponse(response(202), {
      status: "running",
      job_id: "task-1-9",
      retry_after_ms: 3000,
    }),
    {
      summary: null,
      refresh: {
        status: "running",
        jobId: "task-1-9",
        retryAfterMs: 3000,
      },
    }
  );
});

test("preserves a published summary after its refresh fails", () => {
  const summary = { schema_version: 5, summary: "published", components: [] };
  assert.deepEqual(
    parseTrajectorySummaryResponse(response(200), {
      summary,
      refresh: {
        status: "failed",
        job_id: "task-1-9",
        detail: "Trajectory summary refresh failed",
      },
    }),
    {
      summary,
      refresh: {
        status: "failed",
        jobId: "task-1-9",
        detail: "Trajectory summary refresh failed",
      },
    }
  );
});

test("rejects an unknown pending status instead of assuming queued", () => {
  assert.throws(
    () =>
      parseTrajectorySummaryResponse(response(202), {
        summary: null,
        refresh: {
          status: "blocked",
          job_id: "task-1-9",
          retry_after_ms: 3000,
        },
      }),
    /Malformed trajectory summary refresh response/
  );
});

test("rejects a missing job id", () => {
  assert.throws(
    () =>
      parseTrajectorySummaryResponse(response(202), {
        summary: null,
        refresh: { status: "queued", retry_after_ms: 3000 },
      }),
    /Malformed trajectory summary refresh response/
  );
});

test("rejects a non-positive retry interval", () => {
  assert.throws(
    () =>
      parseTrajectorySummaryResponse(response(202), {
        summary: null,
        refresh: {
          status: "queued",
          job_id: "task-1-9",
          retry_after_ms: 0,
        },
      }),
    /Malformed trajectory summary refresh response/
  );
});
