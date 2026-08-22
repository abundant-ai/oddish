import assert from "node:assert/strict";
import test from "node:test";

import { buildDashboardBackendParams } from "../src/lib/dashboard-request.ts";

test("serializes a selected member's stable id through the author parameter", () => {
  const params = buildDashboardBackendParams({
    experiments_author: "user_kyle",
  });

  assert.equal(params.experiments_author, "user_kyle");
});
