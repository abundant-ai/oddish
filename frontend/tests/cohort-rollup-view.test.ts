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
        { model: "a", n: 20, delta: 0.1 },
        { model: "b", n: 12, delta: 0.1 },
        { model: "c", n: 7, delta: 0.1 },
        { model: "d", n: 2, delta: 0.1 },
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
      { category: "debugging", pooled: {}, per_model: [{ model: "d", n: 2, delta: 0.1 }] },
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
      per_model: [{ model: "solo", n: 1, delta: 0.1 }],
    })),
  };
  assert.deepEqual(radarModels(oneTrial), []);
});

test("a model with no drawable vertex is not given a legend row", () => {
  // A one-sided cohort nulls every delta (see the backend `delta`), so
  // ShapeForModel draws nothing. Left in, the model would still take a colour,
  // a legend row and a place in the chart's description: a series the reader
  // is told about and can never find. Seen on real data -- a gemini variant
  // with a 0-success / 9-failure cohort cleared the cited-runs gate with 6.
  const oneSided = {
    thin_threshold: 5,
    models: [
      { model: "mixed", cited_runs: 8 },
      { model: "solo", cited_runs: 6 },
    ],
    categories: [
      {
        category: "debugging",
        pooled: {},
        per_model: [
          { model: "mixed", n: 8, delta: -0.2 },
          { model: "solo", n: 6, delta: null },
        ],
      },
    ],
  };
  assert.deepEqual(
    radarModels(oneSided).map((m) => m.model),
    ["mixed"],
  );
});
