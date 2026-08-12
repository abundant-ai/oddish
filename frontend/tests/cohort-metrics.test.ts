// Run with `pnpm test` (`node --test tests/*.test.ts`). No runner and no
// dependency: Node strips the types, and `cohort-metrics.ts` imports only a
// type from `@/lib/types`, which stripping removes outright.
import { test } from "node:test";
import assert from "node:assert/strict";

import { pooledDeltas, CHART_CATEGORIES } from "../src/lib/cohort-metrics.ts";

const comparison = {
  cohort_success: ["s1", "s2", "s3"],
  cohort_failure: ["f1"],
  categories: [
    {
      category: "debugging",
      label: null,
      successful: [
        {
          behavior_description: "x",
          evidence: [
            { trial_id: "s1", trajectory_component: "c", step_ids: [1], quote: "q" },
            { trial_id: "s1", trajectory_component: "c", step_ids: [2], quote: "q" },
          ],
        },
      ],
      failing: [
        {
          behavior_description: "y",
          evidence: [
            { trial_id: "f1", trajectory_component: "c", step_ids: [3], quote: "q" },
          ],
        },
      ],
    },
    {
      category: "behavior_discovery",
      label: "subagents",
      successful: [
        {
          behavior_description: "z",
          evidence: [
            { trial_id: "s2", trajectory_component: "c", step_ids: [4], quote: "q" },
          ],
        },
      ],
      failing: [],
    },
  ],
};

test("repeated citations of one trial count once", () => {
  const debugging = pooledDeltas(comparison).find((c) => c.category === "debugging");
  assert.equal(debugging.n, 2); // s1 and f1, not three citations
});

test("delta is measured against the cohort's own success share", () => {
  // cohort baseline 3/4 = .75; cited ratio 1/2 = .5
  const debugging = pooledDeltas(comparison).find((c) => c.category === "debugging");
  assert.ok(Math.abs(debugging.delta + 0.25) < 1e-9);
});

test("discovery is not returned", () => {
  assert.equal(
    pooledDeltas(comparison).some((c) => c.category === "behavior_discovery"),
    false,
  );
  assert.equal(CHART_CATEGORIES.length, 6);
});

test("an uncited category is null, not zero", () => {
  const planning = pooledDeltas(comparison).find((c) => c.category === "planning");
  assert.equal(planning.n, 0);
  assert.equal(planning.delta, null);
});

// No failures ran, so nothing can be cited on the failing side either. The
// baseline is 1 and every cited ratio is 1: each delta subtracts to exactly
// zero, and "Where runs diverged" draws six confident +0.00 bars for a cohort
// that had no other side to diverge from.
const oneSided = {
  cohort_success: ["s1", "s2", "s3"],
  cohort_failure: [],
  categories: [
    {
      category: "debugging",
      label: null,
      successful: [
        {
          behavior_description: "x",
          evidence: [
            { trial_id: "s1", trajectory_component: "c", step_ids: [1], quote: "q" },
          ],
        },
      ],
      failing: [],
    },
  ],
};

test("a one-sided cohort has no delta at all", () => {
  const rows = pooledDeltas(oneSided);
  assert.equal(
    rows.every((r) => r.delta === null),
    true,
  );
  // The evidence is still counted; it is the comparison that is undefined.
  assert.equal(rows.find((c) => c.category === "debugging").n, 1);
});
