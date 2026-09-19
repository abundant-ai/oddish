import assert from "node:assert/strict";
import test from "node:test";
import {
  activeFilterCount,
  FILTER_DEFS,
  FILTER_PARAM_KEYS,
  filterParams,
  isFilterActive,
  searchParamsToFilters,
  SORT_OPTIONS,
} from "../src/lib/tasks-filters.ts";

// The stored-summary filters round-trip through the URL exactly like every
// other filter: what filterParams writes, searchParamsToFilters reads back,
// and the sidebar's clear-on-change loop knows every key it wrote.
test("summary threshold filters round-trip through the URL", () => {
  const empty = searchParamsToFilters(new URLSearchParams());
  assert.equal(empty.stepsP50Min, null);
  assert.equal(empty.stepsP50Max, null);
  assert.equal(empty.agentCountMin, null);
  assert.equal(activeFilterCount(empty), 0);

  const values = {
    ...empty,
    stepsP50Min: 400,
    stepsP50Max: null,
    agentCountMin: 3,
    sort: "steps_p50_desc",
  };
  const params = new URLSearchParams(filterParams(values));
  assert.equal(params.get("steps_p50_min"), "400");
  assert.equal(params.has("steps_p50_max"), false);
  assert.equal(params.get("agent_count_min"), "3");
  assert.equal(params.get("sort"), "steps_p50_desc");
  for (const key of params.keys()) {
    assert.ok(
      (FILTER_PARAM_KEYS as readonly string[]).includes(key),
      `${key} must be a known filter param so the sidebar can clear it`
    );
  }

  assert.deepEqual(searchParamsToFilters(params), values);
  for (const key of ["stepsP50", "agentCount", "sort"]) {
    assert.ok(isFilterActive(key, values), `${key} should read as active`);
    assert.ok(
      FILTER_DEFS.some((def) => def.key === key),
      `${key} needs a sidebar registry entry`
    );
  }
  assert.equal(activeFilterCount(values), 3);
});

test("stored-summary sorts are offered", () => {
  const tokens = SORT_OPTIONS.map((o) => o.value);
  for (const token of [
    "steps_p50_desc",
    "steps_p50_asc",
    "total_trials_desc",
    "total_trials_asc",
    "agent_count_desc",
    "agent_count_asc",
  ]) {
    assert.ok(tokens.includes(token), token);
  }
});
