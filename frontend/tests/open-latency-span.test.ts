import assert from "node:assert/strict";
import test from "node:test";

import {
  MAX_PAGE_LOAD_ATTRIBUTION_MS,
  resolveStartTime,
} from "../src/lib/use-open-latency-span.ts";

const TIME_ORIGIN = 1_700_000_000_000;

function input(overrides: Partial<Parameters<typeof resolveStartTime>[0]> = {}) {
  return {
    now: TIME_ORIGIN + 2_000,
    timeOrigin: TIME_ORIGIN,
    navigationType: "navigate",
    firstOpenOnPage: true,
    ...overrides,
  };
}

test("backdates the first open after a hard navigation to page load", () => {
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

test("later opens on the same page use the interaction clock", () => {
  const now = TIME_ORIGIN + 9_000;
  const resolved = resolveStartTime(input({ now, firstOpenOnPage: false }));
  assert.equal(resolved.startTime, now);
  assert.equal(resolved.source, "interaction");
});

test("client-side route changes use the interaction clock", () => {
  // A soft navigation has no document request to account for, so mount is the
  // moment the person asked for the view.
  const now = TIME_ORIGIN + 4_000;
  const resolved = resolveStartTime(input({ now, navigationType: null }));
  assert.equal(resolved.startTime, now);
  assert.equal(resolved.source, "interaction");
});

test("refuses page-load attribution beyond the plausibility ceiling", () => {
  // A drawer opened ten minutes into a session is not describing page load.
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
  const resolved = resolveStartTime(
    input({ now, maxAttributableMs: 1_000 })
  );
  assert.equal(resolved.source, "interaction");
});
