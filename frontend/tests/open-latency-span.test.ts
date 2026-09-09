import assert from "node:assert/strict";
import test from "node:test";

import {
  MAX_PAGE_LOAD_ATTRIBUTION_MS,
  resolveStartTime,
} from "../src/lib/use-open-latency-span.ts";

const TIME_ORIGIN = 1_700_000_000_000;
const TASK_PATH = "/tasks/implement-gofumpt-c6e69524";

function input(overrides: Partial<Parameters<typeof resolveStartTime>[0]> = {}) {
  return {
    now: TIME_ORIGIN + 2_000,
    timeOrigin: TIME_ORIGIN,
    navigationType: "navigate",
    firstOpenOnPage: true,
    initialPath: TASK_PATH,
    currentPath: TASK_PATH,
    ...overrides,
  };
}

test("backdates a landing open to page load", () => {
  const resolved = resolveStartTime(input());
  assert.equal(resolved.startTime, TIME_ORIGIN);
  assert.equal(resolved.source, "page-load");
});

test("counts reload and back_forward as hard navigations", () => {
  for (const navigationType of ["reload", "back_forward"]) {
    const resolved = resolveStartTime(input({ navigationType }));
    assert.equal(resolved.source, "page-load", navigationType);
  }
});

test("a client-side hop to another route uses the interaction clock", () => {
  // The regression this guards: PerformanceNavigationTiming.type describes the
  // DOCUMENT and stays "navigate" for the whole single-page session. Someone
  // who lands on the task list, browses, then opens a task would otherwise have
  // their browsing time recorded as task-open latency.
  const now = TIME_ORIGIN + 20_000;
  const resolved = resolveStartTime(
    input({ now, initialPath: "/tasks", currentPath: TASK_PATH })
  );
  assert.equal(resolved.startTime, now);
  assert.equal(resolved.source, "interaction");
});

test("later opens on the landing route still use the interaction clock", () => {
  const now = TIME_ORIGIN + 9_000;
  const resolved = resolveStartTime(input({ now, firstOpenOnPage: false }));
  assert.equal(resolved.startTime, now);
  assert.equal(resolved.source, "interaction");
});

test("a deep link straight to the task page counts as a landing", () => {
  const resolved = resolveStartTime(
    input({ initialPath: TASK_PATH, currentPath: TASK_PATH })
  );
  assert.equal(resolved.source, "page-load");
});

test("missing path information declines page-load attribution", () => {
  for (const paths of [
    { initialPath: null, currentPath: TASK_PATH },
    { initialPath: TASK_PATH, currentPath: null },
    { initialPath: null, currentPath: null },
  ]) {
    const resolved = resolveStartTime(input(paths));
    assert.equal(resolved.source, "interaction", JSON.stringify(paths));
  }
});

test("a null navigation type uses the interaction clock", () => {
  const now = TIME_ORIGIN + 4_000;
  const resolved = resolveStartTime(input({ now, navigationType: null }));
  assert.equal(resolved.startTime, now);
  assert.equal(resolved.source, "interaction");
});

test("refuses page-load attribution beyond the plausibility ceiling", () => {
  const now = TIME_ORIGIN + MAX_PAGE_LOAD_ATTRIBUTION_MS + 1;
  const resolved = resolveStartTime(input({ now }));
  assert.equal(resolved.startTime, now);
  assert.equal(resolved.source, "interaction");
});

test("keeps page-load attribution exactly at the ceiling", () => {
  const now = TIME_ORIGIN + MAX_PAGE_LOAD_ATTRIBUTION_MS;
  const resolved = resolveStartTime(input({ now }));
  assert.equal(resolved.startTime, TIME_ORIGIN);
  assert.equal(resolved.source, "page-load");
});

test("ignores an unusable time origin", () => {
  const now = TIME_ORIGIN + 2_000;
  for (const timeOrigin of [0, -1, Number.NaN, now + 1_000]) {
    const resolved = resolveStartTime(input({ now, timeOrigin }));
    assert.equal(resolved.startTime, now, String(timeOrigin));
    assert.equal(resolved.source, "interaction", String(timeOrigin));
  }
});

test("honours a caller-supplied ceiling", () => {
  const now = TIME_ORIGIN + 5_000;
  const resolved = resolveStartTime(input({ now, maxAttributableMs: 1_000 }));
  assert.equal(resolved.source, "interaction");
});
