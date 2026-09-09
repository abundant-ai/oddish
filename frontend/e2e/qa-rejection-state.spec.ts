import { expect, test } from "@playwright/test";
import {
  rejectedMustFixLabel,
  taskHasRejectedVerdict,
} from "../src/lib/job-status";
import type { Task } from "../src/lib/types";

const rejected = {
  status: "completed",
  verdict_status: "success",
  verdict: { verdict: "reject", is_good: false },
  trials: [],
} as unknown as Task;

test("published and legacy rejections are reviewable", () => {
  expect(taskHasRejectedVerdict(rejected)).toBe(true);
  expect(
    taskHasRejectedVerdict({
      ...rejected,
      verdict: { is_good: false } as Task["verdict"],
    })
  ).toBe(true);
});

test("acceptance, missing evidence and failed QA are not rejections", () => {
  expect(
    taskHasRejectedVerdict({
      ...rejected,
      verdict: { verdict: "accept", is_good: true } as Task["verdict"],
    })
  ).toBe(false);
  expect(taskHasRejectedVerdict({ ...rejected, verdict: null })).toBe(false);
  expect(
    taskHasRejectedVerdict({ ...rejected, verdict_status: "failed" })
  ).toBe(false);
});

for (const status of ["queued", "running"] as const) {
  test(`replacement QA ${status} hides an old rejection`, () => {
    expect(
      taskHasRejectedVerdict({ ...rejected, verdict_status: status })
    ).toBe(false);
    expect(
      taskHasRejectedVerdict({
        ...rejected,
        active_qa_trial: { kind: "qa", status } as Task["active_qa_trial"],
      })
    ).toBe(false);
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
    taskHasRejectedVerdict({
      ...rejected,
      verdict: null,
      trials: [
        { kind: "agent", status: "failed", reward: 0 },
      ] as Task["trials"],
    })
  ).toBe(false);
});
