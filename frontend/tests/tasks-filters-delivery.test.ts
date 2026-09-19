import assert from "node:assert/strict";
import test from "node:test";
import {
  activeFilterCount,
  FILTER_DEFS,
  FILTER_PARAM_KEYS,
  filterParams,
  isFilterActive,
  searchParamsToFilters,
} from "../src/lib/tasks-filters.ts";

// The delivery-selection filters round-trip through the URL exactly like
// every other filter: what filterParams writes, searchParamsToFilters reads
// back, and the sidebar's clear-on-change loop knows every key it wrote.
test("delivery selection filters round-trip through the URL", () => {
  const empty = searchParamsToFilters(new URLSearchParams());
  assert.deepEqual(empty.deliveredTo, []);
  assert.deepEqual(empty.notDeliveredTo, []);
  assert.equal(empty.neverDelivered, null);
  assert.deepEqual(empty.categories, []);
  assert.equal(activeFilterCount(empty), 0);

  const values = {
    ...empty,
    deliveredTo: ["GDM"],
    notDeliveredTo: ["xai", "TML"],
    neverDelivered: false,
    categories: ["security"],
  };
  const params = new URLSearchParams(filterParams(values));
  assert.equal(params.get("delivered_to"), "GDM");
  assert.equal(params.get("not_delivered_to"), "xai,TML");
  assert.equal(params.get("never_delivered"), "false");
  assert.equal(params.get("categories"), "security");
  for (const key of params.keys()) {
    assert.ok(
      (FILTER_PARAM_KEYS as readonly string[]).includes(key),
      `${key} must be a known filter param so the sidebar can clear it`
    );
  }

  assert.deepEqual(searchParamsToFilters(params), values);
  for (const key of [
    "deliveredTo",
    "notDeliveredTo",
    "neverDelivered",
    "categories",
  ]) {
    assert.ok(isFilterActive(key, values), `${key} should read as active`);
    assert.ok(
      FILTER_DEFS.some((def) => def.key === key),
      `${key} needs a sidebar registry entry`
    );
  }
  assert.equal(activeFilterCount(values), 4);
});

test("delivery filters sit in their own Add-filter group", () => {
  const groups = new Set(
    FILTER_DEFS.filter((def) =>
      ["deliveredTo", "notDeliveredTo", "neverDelivered"].includes(def.key)
    ).map((def) => def.group)
  );
  assert.deepEqual([...groups], ["Delivery"]);
});
