import assert from "node:assert/strict";
import test from "node:test";

import {
  MAX_INTENT_AGE_MS,
  clearOpenIntents,
  markOpenIntent,
  resolveInteractionStart,
  takeOpenIntent,
} from "../src/lib/open-intent.ts";

const CLICK = 1_700_000_000_000;

test("a delayed mount still counts from the click", () => {
  // The regression this exists for: clicking a task has to download the route
  // chunk and render before `ui.task.open` mounts. Two seconds of that plus a
  // hundred milliseconds of data must record 2.1s, not 0.1s.
  const resolved = resolveInteractionStart({
    intentAt: CLICK,
    mountedAt: CLICK + 2_000,
  });
  assert.equal(resolved.startTime, CLICK);
  assert.equal(resolved.source, "click");

  const paintedAt = CLICK + 2_100;
  assert.equal(paintedAt - resolved.startTime, 2_100);
});

test("falls back to mount when no click was recorded", () => {
  const mountedAt = CLICK + 500;
  const resolved = resolveInteractionStart({ intentAt: null, mountedAt });
  assert.equal(resolved.startTime, mountedAt);
  assert.equal(resolved.source, "mount");
});

test("ignores a click too old to have caused this mount", () => {
  const mountedAt = CLICK + MAX_INTENT_AGE_MS + 1;
  const resolved = resolveInteractionStart({ intentAt: CLICK, mountedAt });
  assert.equal(resolved.startTime, mountedAt);
  assert.equal(resolved.source, "mount");
});

test("keeps a click exactly at the age ceiling", () => {
  const mountedAt = CLICK + MAX_INTENT_AGE_MS;
  const resolved = resolveInteractionStart({ intentAt: CLICK, mountedAt });
  assert.equal(resolved.startTime, CLICK);
  assert.equal(resolved.source, "click");
});

test("ignores a click that postdates the mount it supposedly caused", () => {
  const mountedAt = CLICK - 1;
  const resolved = resolveInteractionStart({ intentAt: CLICK, mountedAt });
  assert.equal(resolved.startTime, mountedAt);
  assert.equal(resolved.source, "mount");
});

test("ignores a non-finite click timestamp", () => {
  const mountedAt = CLICK + 100;
  const resolved = resolveInteractionStart({
    intentAt: Number.NaN,
    mountedAt,
  });
  assert.equal(resolved.startTime, mountedAt);
  assert.equal(resolved.source, "mount");
});

test("an intent is delivered to its own name and subject only", () => {
  clearOpenIntents();
  markOpenIntent("ui.task.open", "task-a", CLICK);

  assert.equal(takeOpenIntent("ui.task.open", "task-b"), null);
  assert.equal(takeOpenIntent("ui.files.open", "task-a"), null);

  const found = takeOpenIntent("ui.task.open", "task-a");
  assert.equal(found?.at, CLICK);
});

test("an intent is consumed, so a remount cannot reuse it", () => {
  clearOpenIntents();
  markOpenIntent("ui.task.open", "task-a", CLICK);

  assert.equal(takeOpenIntent("ui.task.open", "task-a")?.at, CLICK);
  assert.equal(takeOpenIntent("ui.task.open", "task-a"), null);
});

test("concurrent intents for different subjects do not collide", () => {
  clearOpenIntents();
  markOpenIntent("ui.task.open", "task-a", CLICK);
  markOpenIntent("ui.task.open", "task-b", CLICK + 50);

  assert.equal(takeOpenIntent("ui.task.open", "task-b")?.at, CLICK + 50);
  assert.equal(takeOpenIntent("ui.task.open", "task-a")?.at, CLICK);
});
