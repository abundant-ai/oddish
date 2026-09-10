import { expect, test } from "@playwright/test";
import { rejectedMustFixLabel } from "../src/lib/job-status";
import { taskReviewFilter } from "../src/lib/review";
import type { Task } from "../src/lib/types";

const rejected = {
  status: "completed",
  verdict_status: "success",
  verdict: { verdict: "reject", is_good: false },
  trials: [],
} as unknown as Task;

test("published and legacy rejections are reviewable", () => {
  expect(taskReviewFilter(rejected)).toBe("rejected");
  expect(
    taskReviewFilter({
      ...rejected,
      verdict: { is_good: false } as Task["verdict"],
    })
  ).toBe("rejected");
});

test("acceptance, missing evidence and failed QA are not rejections", () => {
  expect(
    taskReviewFilter({
      ...rejected,
      verdict: { verdict: "accept", is_good: true } as Task["verdict"],
    })
  ).not.toBe("rejected");
  expect(taskReviewFilter({ ...rejected, verdict: null })).not.toBe("rejected");
  expect(taskReviewFilter({ ...rejected, verdict_status: "failed" })).not.toBe(
    "rejected"
  );
});

for (const status of ["queued", "running"] as const) {
  test(`replacement QA ${status} hides an old rejection`, () => {
    expect(taskReviewFilter({ ...rejected, verdict_status: status })).not.toBe(
      "rejected"
    );
    expect(
      taskReviewFilter({
        ...rejected,
        active_qa_trial: { kind: "qa", status } as Task["active_qa_trial"],
      })
    ).not.toBe("rejected");
  });
}

test("rejected experiment copy uses the must-fix count", () => {
  expect(rejectedMustFixLabel(rejected)).toBe("Rejected");
  expect(
    rejectedMustFixLabel({
      ...rejected,
      must_fix_count: 1,
    })
  ).toBe("1 Must Fix");
  expect(
    rejectedMustFixLabel({
      ...rejected,
      must_fix_count: 3,
    })
  ).toBe("3 Must Fix");
});

test("a failing solver run alone does not reject a task", () => {
  expect(
    taskReviewFilter({
      ...rejected,
      verdict: null,
      trials: [
        { kind: "agent", status: "failed", reward: 0 },
      ] as Task["trials"],
    })
  ).not.toBe("rejected");
});
