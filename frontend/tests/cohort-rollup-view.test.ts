import { test } from "node:test";
import assert from "node:assert/strict";

import { radarModels, RADAR_MODEL_CAP } from "../src/lib/cohort-metrics.ts";

const rollup = {
  thin_threshold: 5,
  models: [
    { model: "a", cited_runs: 20 },
    { model: "b", cited_runs: 12 },
    { model: "c", cited_runs: 7 },
    { model: "d", cited_runs: 2 },
  ],
  categories: [
    {
      category: "debugging",
      pooled: {},
      per_model: [
        { model: "a", n: 20 },
        { model: "b", n: 12 },
        { model: "c", n: 7 },
        { model: "d", n: 2 },
      ],
    },
  ],
};

test("caps the radar at three models by total n", () => {
  assert.deepEqual(
    radarModels(rollup).map((m) => m.model),
    ["a", "b", "c"],
  );
  assert.equal(RADAR_MODEL_CAP, 3);
});

test("drops a model below the thin threshold even inside the cap", () => {
  const thin = {
    ...rollup,
    models: [{ model: "d", cited_runs: 2 }],
    categories: [
      { category: "debugging", pooled: {}, per_model: [{ model: "d", n: 2 }] },
    ],
  };
  assert.deepEqual(radarModels(thin), []);
});

test("one trial cited in six categories does not clear the gate", () => {
  // Each per-category `n` is a distinct-trial count *within* that category, so
  // summing them totals six for a single run cited across all six -- which is
  // how one trajectory used to earn a closed, filled shape on every axis.
  const oneTrial = {
    thin_threshold: 5,
    models: [{ model: "solo", cited_runs: 1 }],
    categories: Array.from({ length: 6 }, (_, i) => ({
      category: `c${i}`,
      pooled: {},
      per_model: [{ model: "solo", n: 1 }],
    })),
  };
  assert.deepEqual(radarModels(oneTrial), []);
});
