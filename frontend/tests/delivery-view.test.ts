import assert from "node:assert/strict";
import test from "node:test";
import { parseDeliveryView, deliveryViewQuery } from "../src/lib/deliveries.ts";

test("shared links restore every delivery filter and the focused task", () => {
  const query = deliveryViewQuery("?source=slack", {
    page: "3",
    filter: "blocked",
    days: "1",
    qa: "needs_fixes",
    issue: "verifier",
    owner: "mine",
    group: "owner",
    task: "task / + &",
  });
  assert.deepEqual(parseDeliveryView(new URLSearchParams(query)), {
    page: 2,
    pageSize: 25,
    filter: "blocked",
    qaDays: "1",
    qaFilter: "needs_fixes",
    issueFilter: "verifier",
    ownerFilter: "mine",
    groupBy: "owner",
    focusTask: "task / + &",
  });
  assert.equal(new URLSearchParams(query).get("source"), "slack");
});

test("invalid external URL values fall back to the default view", () => {
  const defaults = parseDeliveryView(new URLSearchParams());
  for (const page of [
    "0",
    "-1",
    "1.5",
    "1e2",
    "Infinity",
    "9007199254740992",
    "abc",
    "",
  ]) {
    assert.deepEqual(
      parseDeliveryView(
        new URLSearchParams({
          page,
          filter: "unknown",
          days: "-7",
          qa: "constructor",
          issue: "__proto__",
          owner: "stranger",
          group: "invalid",
          task: "",
        })
      ),
      defaults
    );
  }
});

test("filter changes reset pagination and task focus without losing other filters", () => {
  const next = deliveryViewQuery("?page=8&task=old&owner=mine&qa=never", {
    qa: "accepted",
    page: null,
    task: null,
  });
  const view = parseDeliveryView(new URLSearchParams(next));
  assert.equal(view.page, 0);
  assert.equal(view.focusTask, null);
  assert.equal(view.ownerFilter, "mine");
  assert.equal(view.qaFilter, "accepted");
});

test("defaults produce a clean URL and history entries can restore prior views", () => {
  const first = "?page=2&qa=never&task=abc";
  const second = deliveryViewQuery(first, { page: "3", task: null });
  assert.equal(parseDeliveryView(new URLSearchParams(second)).page, 2);
  assert.equal(parseDeliveryView(new URLSearchParams(first)).focusTask, "abc");
  assert.equal(deliveryViewQuery(second, { page: "1", qa: "all" }), "");
});

test("page sizes survive shared links and history without losing filters", () => {
  for (const size of [10, 25, 50, 100]) {
    const query = deliveryViewQuery(
      "?page=8&task=old&owner=mine&source=slack",
      {
        per_page: String(size),
        page: null,
        task: null,
      }
    );
    const view = parseDeliveryView(new URLSearchParams(query));
    assert.equal(view.pageSize, size);
    assert.equal(view.page, 0);
    assert.equal(view.focusTask, null);
    assert.equal(view.ownerFilter, "mine");
    assert.equal(new URLSearchParams(query).get("source"), "slack");
    assert.equal(new URLSearchParams(query).has("per_page"), size !== 25);
  }
  assert.equal(deliveryViewQuery("?per_page=100", { per_page: "25" }), "");
});

test("unsupported page sizes fall back to 25 rows", () => {
  for (const size of ["", "0", "-1", "1.5", "20", "Infinity", "abc"]) {
    assert.equal(
      parseDeliveryView(new URLSearchParams({ per_page: size })).pageSize,
      25
    );
  }
});
