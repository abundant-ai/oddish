import assert from "node:assert/strict";
import test from "node:test";

import {
  buildPublicQaHref,
  experimentQaFormField,
  experimentQaSignal,
} from "../src/lib/experiment-qa.ts";

test("maps task verdict outcomes to the public QA signal", () => {
  assert.equal(
    experimentQaSignal({
      outcome: "accept",
      tier: null,
      source_type: "verdict",
    }),
    "valid"
  );
  assert.equal(
    experimentQaSignal({
      outcome: "reject",
      tier: null,
      source_type: "verdict",
    }),
    "needs_work"
  );
  assert.equal(
    experimentQaSignal({
      outcome: "BAD_SUCCESS",
      tier: null,
      source_type: "trial_analysis",
    }),
    "false_positive"
  );
  assert.equal(
    experimentQaSignal({
      outcome: "HARNESS_ERROR",
      tier: null,
      source_type: "trial_analysis",
    }),
    "harness"
  );
});

test("builds a scoped public QA URL with encoded tokens", () => {
  assert.equal(
    buildPublicQaHref("experiment/token", "qa token?1"),
    "/share/experiment%2Ftoken/qa?t=qa%20token%3F1"
  );
});

test("keeps report form keys stable for opaque row ids", () => {
  assert.equal(
    experimentQaFormField("item", "row/id 1", "summary"),
    "item.row%2Fid%201.summary"
  );
});
