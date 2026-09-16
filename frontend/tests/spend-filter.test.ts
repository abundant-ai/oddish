import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";
import type { CostSeries } from "../src/lib/types.ts";

const cache: Record<string, unknown> = {};
function load(name: string): unknown {
  if (name in cache) return cache[name];
  assert.ok(name.startsWith("@/lib/"), name);
  const exports = {};
  cache[name] = exports;
  runInNewContext(
    ts.transpileModule(
      readFileSync(
        new URL(`../src/${name.slice(2)}.ts`, import.meta.url),
        "utf8",
      ),
      { compilerOptions: { module: ts.ModuleKind.CommonJS } },
    ).outputText,
    { exports, require: load },
  );
  return exports;
}

const spend = load(
  "@/lib/spend-filter",
) as typeof import("../src/lib/spend-filter.ts");

function series(partial: Partial<CostSeries> & Pick<CostSeries, "keys" | "buckets">): CostSeries {
  return {
    dimension: partial.dimension ?? "test",
    keys: partial.keys,
    buckets: partial.buckets,
  };
}

test("buildSpendGroups always lists Modal and Thunder under compute", () => {
  const groups = spend.buildSpendGroups(
    series({
      keys: [{ key: "anthropic/claude", label: "anthropic/claude" }],
      buckets: [
        {
          bucket_start: "2026-01-01T00:00:00Z",
          cost_usd: 10,
          trial_count: 1,
          costs: { "anthropic/claude": 10 },
        },
      ],
    }),
    series({
      keys: [{ key: "modal", label: "Modal" }],
      buckets: [
        {
          bucket_start: "2026-01-01T00:00:00Z",
          cost_usd: 2,
          trial_count: 0,
          costs: { modal: 2 },
        },
      ],
    }),
  );

  assert.equal(groups[0].id, "compute");
  assert.equal(
    groups[0].options.map((o) => o.key).join(","),
    "modal,thunder",
  );
  assert.equal(groups[0].options[0].costUsd, 2);
  assert.equal(groups[0].options[1].costUsd, 0);
  assert.equal(groups[1].options[0].key, "anthropic/claude");
});

test("defaultSpendSelection prefers options with spend", () => {
  const groups = spend.buildSpendGroups(
    series({
      keys: [
        { key: "a", label: "a" },
        { key: "b", label: "b" },
      ],
      buckets: [
        {
          bucket_start: "2026-01-01T00:00:00Z",
          cost_usd: 5,
          trial_count: 1,
          costs: { a: 5, b: 0 },
        },
      ],
    }),
    series({
      keys: [
        { key: "modal", label: "Modal" },
        { key: "thunder", label: "Thunder Compute" },
      ],
      buckets: [
        {
          bucket_start: "2026-01-01T00:00:00Z",
          cost_usd: 3,
          trial_count: 0,
          costs: { thunder: 3 },
        },
      ],
    }),
  );

  const selected = spend.defaultSpendSelection(groups);
  assert.equal(selected.has(spend.modelOptionId("a")), true);
  assert.equal(selected.has(spend.modelOptionId("b")), false);
  assert.equal(selected.has(spend.computeOptionId("thunder")), true);
  assert.equal(selected.has(spend.computeOptionId("modal")), false);
});

test("mergeSpendSeries stacks selected model and compute keys", () => {
  const merged = spend.mergeSpendSeries(
    series({
      keys: [{ key: "gpt", label: "gpt" }],
      buckets: [
        {
          bucket_start: "2026-01-01T00:00:00Z",
          cost_usd: 4,
          trial_count: 2,
          costs: { gpt: 4 },
        },
      ],
    }),
    series({
      keys: [
        { key: "modal", label: "Modal" },
        { key: "thunder", label: "Thunder Compute" },
      ],
      buckets: [
        {
          bucket_start: "2026-01-01T00:00:00Z",
          cost_usd: 1.5,
          trial_count: 0,
          costs: { modal: 1, thunder: 0.5 },
        },
      ],
    }),
    new Set([
      spend.modelOptionId("gpt"),
      spend.computeOptionId("thunder"),
    ]),
  );

  assert.equal(merged.dimension, "spend");
  assert.equal(
    merged.keys.map((k) => k.key).join(","),
    `${spend.computeOptionId("thunder")},${spend.modelOptionId("gpt")}`,
  );
  assert.equal(merged.buckets[0].costs[spend.modelOptionId("gpt")], 4);
  assert.equal(merged.buckets[0].costs[spend.computeOptionId("thunder")], 0.5);
  assert.equal(merged.buckets[0].costs[spend.computeOptionId("modal")], undefined);
  assert.equal(merged.buckets[0].cost_usd, 4.5);
});

test("spendEmptyMessage explains Modal-only idle during Thunder migration", () => {
  const groups = spend.buildSpendGroups(
    series({ keys: [], buckets: [] }),
    series({
      keys: [{ key: "thunder", label: "Thunder Compute" }],
      buckets: [
        {
          bucket_start: "2026-01-01T00:00:00Z",
          cost_usd: 2,
          trial_count: 0,
          costs: { thunder: 2 },
        },
      ],
    }),
  );
  const empty = spend.mergeSpendSeries(
    series({ keys: [], buckets: [] }),
    series({
      keys: [{ key: "thunder", label: "Thunder Compute" }],
      buckets: [
        {
          bucket_start: "2026-01-01T00:00:00Z",
          cost_usd: 2,
          trial_count: 0,
          costs: { thunder: 2 },
        },
      ],
    }),
    new Set([spend.computeOptionId("modal")]),
  );
  const message = spend.spendEmptyMessage(
    groups,
    new Set([spend.computeOptionId("modal")]),
    empty,
  );
  assert.match(message ?? "", /No Modal compute spend/);
});

test("computeTransitionState labels Modal → Thunder shift", () => {
  const both = spend.computeTransitionState(
    series({
      keys: [
        { key: "modal", label: "Modal" },
        { key: "thunder", label: "Thunder Compute" },
      ],
      buckets: [
        {
          bucket_start: "2026-01-01T00:00:00Z",
          cost_usd: 5,
          trial_count: 0,
          costs: { modal: 2, thunder: 3 },
        },
      ],
    }),
  );
  assert.match(both.label ?? "", /both reporting/);

  const thunderOnly = spend.computeTransitionState(
    series({
      keys: [{ key: "thunder", label: "Thunder Compute" }],
      buckets: [
        {
          bucket_start: "2026-01-01T00:00:00Z",
          cost_usd: 3,
          trial_count: 0,
          costs: { thunder: 3 },
        },
      ],
    }),
  );
  assert.match(thunderOnly.label ?? "", /Compute on Thunder/);
});

test("summarizeSpendSelection stays concise", () => {
  const groups = spend.buildSpendGroups(
    series({
      keys: [
        { key: "a", label: "a" },
        { key: "b", label: "b" },
      ],
      buckets: [],
    }),
    series({
      keys: [
        { key: "modal", label: "Modal" },
        { key: "thunder", label: "Thunder Compute" },
      ],
      buckets: [],
    }),
  );
  assert.equal(
    spend.summarizeSpendSelection(
      groups,
      new Set([
        spend.computeOptionId("modal"),
        spend.computeOptionId("thunder"),
        spend.modelOptionId("a"),
        spend.modelOptionId("b"),
      ]),
    ),
    "All spend",
  );
  assert.equal(
    spend.summarizeSpendSelection(
      groups,
      new Set([
        spend.computeOptionId("modal"),
        spend.modelOptionId("a"),
        spend.modelOptionId("b"),
      ]),
    ),
    "Modal · 2 models",
  );
});
