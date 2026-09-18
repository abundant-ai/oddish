import assert from "node:assert/strict";
import test from "node:test";

import {
  overlayVerifierSeries,
  VERIFIER_OVERLAY_KEY,
  VERIFIER_OVERLAY_LABEL,
} from "../src/lib/cost-chart-series.ts";
import type { CostSeries } from "../src/lib/types.ts";

function series(
  dimension: string,
  buckets: CostSeries["buckets"],
  keys: CostSeries["keys"]
): CostSeries {
  return { dimension, keys, buckets };
}

test("overlayVerifierSeries adds a CUA segment on days that have verifier spend", () => {
  const agent = series(
    "agent",
    [
      {
        bucket_start: "2026-09-14T00:00:00Z",
        cost_usd: 15,
        trial_count: 2,
        costs: { oracle: 10, "claude-code": 5 },
      },
    ],
    [
      { key: "oracle", label: "oracle" },
      { key: "claude-code", label: "claude-code" },
    ]
  );
  const type = series(
    "type",
    [
      {
        bucket_start: "2026-09-14T00:00:00Z",
        cost_usd: 26,
        trial_count: 2,
        costs: { inference: 15, qa: 1, verifier: 10 },
      },
      {
        bucket_start: "2026-09-15T00:00:00Z",
        cost_usd: 8,
        trial_count: 0,
        costs: { verifier: 8 },
      },
    ],
    [{ key: "verifier", label: "Verifier" }]
  );

  const overlay = overlayVerifierSeries(agent, type);
  assert.equal(overlay.dimension, "agent");
  assert.deepEqual(overlay.keys.at(-1), {
    key: VERIFIER_OVERLAY_KEY,
    label: VERIFIER_OVERLAY_LABEL,
  });
  assert.equal(overlay.buckets.length, 2);
  assert.equal(overlay.buckets[0]?.costs[VERIFIER_OVERLAY_KEY], 10);
  assert.equal(overlay.buckets[0]?.cost_usd, 25);
  assert.equal(overlay.buckets[0]?.trial_count, 2);
  assert.equal(overlay.buckets[1]?.costs[VERIFIER_OVERLAY_KEY], 8);
  assert.equal(overlay.buckets[1]?.cost_usd, 8);
  assert.equal(overlay.buckets[1]?.trial_count, 0);
});

test("overlayVerifierSeries is a no-op on the type series or when CUA is zero", () => {
  const type = series(
    "type",
    [
      {
        bucket_start: "2026-09-14T00:00:00Z",
        cost_usd: 10,
        trial_count: 1,
        costs: { inference: 10, verifier: 4 },
      },
    ],
    [
      { key: "inference", label: "Model inference" },
      { key: "verifier", label: "Verifier" },
    ]
  );
  assert.equal(overlayVerifierSeries(type, type), type);

  const agent = series(
    "agent",
    [
      {
        bucket_start: "2026-09-14T00:00:00Z",
        cost_usd: 3,
        trial_count: 1,
        costs: { oracle: 3 },
      },
    ],
    [{ key: "oracle", label: "oracle" }]
  );
  const emptyType = series(
    "type",
    [
      {
        bucket_start: "2026-09-14T00:00:00Z",
        cost_usd: 3,
        trial_count: 1,
        costs: { inference: 3 },
      },
    ],
    [{ key: "inference", label: "Model inference" }]
  );
  assert.equal(overlayVerifierSeries(agent, emptyType), agent);
  assert.equal(overlayVerifierSeries(agent, undefined), agent);
});
