import test from "node:test";
import assert from "node:assert/strict";

import { checkTone, readySummary } from "../src/lib/deliveries.ts";
import type { DeliveryBoardResponse } from "../src/lib/types.ts";

const board = (overrides: Partial<DeliveryBoardResponse>) =>
  ({
    ready: false,
    ready_task_count: 2,
    task_count: 5,
    delivery_checks: [],
    tasks: [],
    ...overrides,
  }) as DeliveryBoardResponse;

test("checkTone maps statuses to distinct tones", () => {
  assert.notEqual(checkTone("pass"), checkTone("fail"));
  assert.match(checkTone("pass"), /emerald/);
  assert.match(checkTone("fail"), /red/);
  assert.match(checkTone("off"), /muted/);
});

test("readySummary counts tasks", () => {
  assert.equal(readySummary(board({})), "2/5 tasks ready");
});

test("readySummary reports open delivery checks", () => {
  const summary = readySummary(
    board({
      delivery_checks: [
        {
          key: "scope_ok",
          kind: "manual",
          label: "Scope confirmed",
          status: "fail",
          detail: "not checked",
        },
      ],
    })
  );
  assert.equal(summary, "2/5 tasks ready · 1 delivery check open");
});
