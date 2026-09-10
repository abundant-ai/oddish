import assert from "node:assert/strict";
import test from "node:test";
import { deliveryOwnerOutcome } from "../src/lib/deliveries.ts";
import { taskRow } from "../e2e/delivery-fixtures.ts";

const rejected = () => {
  const row = taskRow();
  row.qa.status = "needs_fixes";
  row.defects = [
    {
      id: "finding",
      title: "Known defect",
      source: "pre_trial",
      acknowledged: true,
    },
  ];
  return row;
};

test("grey requires rejected QA, acknowledged findings and current-version human sign-off", () => {
  const row = rejected();
  assert.deepEqual(deliveryOwnerOutcome(row), {
    status: "needs_work",
    signedOff: false,
  });
  row.checks[1].status = "pass";
  assert.deepEqual(deliveryOwnerOutcome(row), {
    status: "accepted_exceptions",
    signedOff: true,
  });
  row.defects.push({
    ...row.defects[0],
    id: "new-finding",
    acknowledged: false,
  });
  assert.deepEqual(deliveryOwnerOutcome(row), {
    status: "needs_work",
    signedOff: true,
  });
});

test("sign-off alone cannot convert rejected QA into accepted exceptions", () => {
  const row = rejected();
  row.checks[1].status = "pass";
  row.defects = [];
  assert.equal(deliveryOwnerOutcome(row).status, "needs_work");
});

test("QA accepted stays green with or without human sign-off", () => {
  const row = taskRow();
  row.qa.status = "accepted";
  assert.deepEqual(deliveryOwnerOutcome(row), {
    status: "qa_accepted",
    signedOff: false,
  });
  row.checks[1].status = "pass";
  row.ready = true;
  assert.deepEqual(deliveryOwnerOutcome(row), {
    status: "qa_accepted",
    signedOff: true,
  });
  row.defects = [
    {
      id: "finding",
      title: "Unresolved defect",
      source: "pre_trial",
      acknowledged: false,
    },
  ];
  assert.equal(deliveryOwnerOutcome(row).status, "needs_work");
});

for (const status of [
  "never",
  "queued",
  "running",
  "error",
  "outdated",
] as const) {
  test(`${status} QA is incomplete, including when an earlier sign-off exists`, () => {
    const row = rejected();
    row.qa.status = status;
    row.checks[1].status = "pass";
    assert.equal(deliveryOwnerOutcome(row).status, "qa_incomplete");
  });
}

test("sign-off on an older version cannot turn acknowledged rejection grey", () => {
  const row = rejected();
  row.checks[1].detail = "signed off on an older version; sign off again";
  assert.deepEqual(deliveryOwnerOutcome(row), {
    status: "needs_work",
    signedOff: false,
  });
});
