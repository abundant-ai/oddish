import assert from "node:assert/strict";
import test from "node:test";
import {
  parseStoredSelection,
  SELECTION_LIMIT,
  selectionStorageKey,
  selectionTotals,
  serializeSelection,
} from "../src/lib/tasks-selection.ts";

// What the provider writes to localStorage it must read back unchanged, and
// anything else in that slot (old shape, garbage) must read as empty.
test("selection survives a localStorage round-trip", () => {
  const selection = new Map([
    ["t-1", { cost: 1.5, estimated: false }],
    ["t-2", { cost: null, estimated: false }],
  ]);
  const back = parseStoredSelection(serializeSelection(selection));
  assert.deepEqual(Array.from(back.entries()), Array.from(selection.entries()));

  assert.equal(parseStoredSelection(null).size, 0);
  assert.equal(parseStoredSelection("not json").size, 0);
  assert.equal(parseStoredSelection('{"v":0,"entries":[["x",{}]]}').size, 0);
  assert.equal(parseStoredSelection('{"v":1,"entries":[7,["x",{}]]}').size, 1);
  assert.equal(selectionStorageKey("org_1"), "oddish.tasks.selection.org_1");
  assert.equal(selectionStorageKey(null), "oddish.tasks.selection.personal");
});

test("stored selection is capped at the select-all ceiling", () => {
  const entries = Array.from({ length: SELECTION_LIMIT + 5 }, (_, i) => [
    `t-${i}`,
    { cost: null, estimated: false },
  ]);
  const back = parseStoredSelection(JSON.stringify({ v: 1, entries }));
  assert.equal(back.size, SELECTION_LIMIT);
});

// The cost total is only claimed when every ticked task carries a cost;
// one id-only entry (from "Select all") withdraws it.
test("cost total needs a cost on every entry", () => {
  const priced = new Map([
    ["a", { cost: 2, estimated: false }],
    ["b", { cost: 3, estimated: true }],
  ]);
  assert.deepEqual(selectionTotals(priced), {
    count: 2,
    cost: 5,
    anyEstimated: true,
  });
  priced.set("c", { cost: null, estimated: false });
  assert.equal(selectionTotals(priced).cost, null);
  assert.equal(selectionTotals(new Map()).cost, null);
});
