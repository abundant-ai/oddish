import assert from "node:assert/strict";
import test from "node:test";
import {
  TASK_COLUMN_KEYS,
  defaultColumnVisibility,
  parseColumnVisibility,
  serializeColumnVisibility,
} from "../src/lib/tasks-columns.ts";

test("a hidden field stays hidden across a reload", () => {
  const visibility = defaultColumnVisibility();
  visibility.cost = false;

  const restored = parseColumnVisibility(serializeColumnVisibility(visibility));

  assert.equal(restored.cost, false);
  assert.equal(restored.lastRun, true);
});

test("showing a field again clears it from storage", () => {
  const hiddenCost = serializeColumnVisibility({
    ...defaultColumnVisibility(),
    cost: false,
  });
  assert.equal(hiddenCost, '["cost"]');

  const reshown = parseColumnVisibility(hiddenCost);
  reshown.cost = true;

  assert.equal(serializeColumnVisibility(reshown), "[]");
  assert.deepEqual(parseColumnVisibility("[]"), defaultColumnVisibility());
});

test("no stored preference shows every field", () => {
  assert.deepEqual(parseColumnVisibility(null), defaultColumnVisibility());
  assert.deepEqual(parseColumnVisibility(""), defaultColumnVisibility());
});

test("a corrupt entry falls back to every field visible", () => {
  // A page that renders no fields at all is worse than ignoring the
  // preference, so every unreadable shape degrades to the default.
  for (const raw of ["not json", "{}", '"cost"', "null", "42"]) {
    assert.deepEqual(
      parseColumnVisibility(raw),
      defaultColumnVisibility(),
      `expected defaults for ${raw}`
    );
  }
});

test("a field added later defaults to visible for existing users", () => {
  // Storage holds hidden keys, so a preference saved before a field existed
  // cannot hide it.
  const savedBeforeNewField = '["cost"]';
  const restored = parseColumnVisibility(savedBeforeNewField);

  for (const key of TASK_COLUMN_KEYS) {
    if (key === "cost") continue;
    assert.equal(restored[key], true, `${key} should default to visible`);
  }
});

test("keys that no longer exist are ignored", () => {
  const restored = parseColumnVisibility('["cost","retiredField"]');

  assert.equal(restored.cost, false);
  assert.equal(Object.keys(restored).length, TASK_COLUMN_KEYS.length);
});
