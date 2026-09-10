import assert from "node:assert/strict";
import test from "node:test";
import { deliveryQAStatus, isDeliveryBlocked } from "../src/lib/deliveries.ts";
import type { DeliveryTaskBoardRow } from "../src/lib/types.ts";

const cutoff = Date.parse("2026-09-01T00:00:00Z");
const row = {
  ready: false,
  qa: {
    status: "accepted",
    finished_at: "2026-09-01T00:00:00Z",
    trial_id: "qa-1",
    detail: "Current evidence",
  },
} as DeliveryTaskBoardRow;

test("accepted and rejected results at the cutoff count as checked", () => {
  for (const status of ["accepted", "needs_fixes"] as const) {
    assert.equal(
      deliveryQAStatus({ ...row, qa: { ...row.qa, status } }, cutoff).status,
      status
    );
  }
});

test("old results and results without completion time are outdated", () => {
  for (const finished_at of ["2026-08-31T23:59:59Z", null]) {
    assert.equal(
      deliveryQAStatus({ ...row, qa: { ...row.qa, finished_at } }, cutoff)
        .status,
      "outdated"
    );
  }
  assert.equal(row.qa.status, "accepted");
});

test("recent timestamps do not turn errors, in-flight runs or stale evidence into checked tasks", () => {
  for (const status of [
    "error",
    "never",
    "queued",
    "running",
    "outdated",
  ] as const) {
    assert.equal(
      deliveryQAStatus({ ...row, qa: { ...row.qa, status } }, cutoff).status,
      status
    );
  }
});

test("delivery blockers come from outstanding checks and findings, not review status or sign-off", () => {
  const base = { ...row, checks: [], defects: [] };
  for (const status of ["accepted", "error", "needs_fixes"] as const) {
    assert.equal(
      isDeliveryBlocked({ ...base, qa: { ...row.qa, status } }),
      false
    );
  }
  const finding = {
    id: "historical",
    acknowledged: false,
    recorded_tier: "should_fix",
  };
  assert.equal(
    isDeliveryBlocked({ ...base, defects: [finding] } as DeliveryTaskBoardRow),
    true
  );
  assert.equal(
    isDeliveryBlocked({
      ...base,
      defects: [{ ...finding, acknowledged: true }],
    } as DeliveryTaskBoardRow),
    false
  );
  for (const status of ["fail", "waived", "pass", "off"] as const) {
    const checks = [
      { key: "min_rollouts", kind: "automated", status },
      { key: "signoff", kind: "manual", status: "pass" },
    ];
    assert.equal(
      isDeliveryBlocked({ ...base, checks } as DeliveryTaskBoardRow),
      status === "fail"
    );
  }
  assert.equal(
    isDeliveryBlocked({
      ...base,
      checks: [{ key: "signoff", kind: "manual", status: "fail" }],
    } as DeliveryTaskBoardRow),
    false
  );
});
