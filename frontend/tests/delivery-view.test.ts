import assert from "node:assert/strict";
import test from "node:test";
import {
  parseDeliveryView,
  deliveryViewQuery,
  deliveryPageQuery,
  deliveryPageContainsView,
} from "../src/lib/deliveries.ts";

test("shared links restore every delivery filter and the focused task", () => {
  const query = deliveryViewQuery("?source=slack", {
    page: "3",
    filter: "needs_work",
    issue: "verifier",
    owner: "mine",
    group: "owner",
    task: "task / + &",
  });
  assert.deepEqual(parseDeliveryView(new URLSearchParams(query)), {
    page: 2,
    pageSize: 25,
    filter: "needs_work",
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
          group: "invalid",
          task: "",
        })
      ),
      defaults
    );
  }
});

test("filter changes reset pagination and task focus without losing other filters", () => {
  const next = deliveryViewQuery(
    "?page=8&task=old&owner=mine&filter=qa_incomplete",
    {
      filter: "ready",
      page: null,
      task: null,
    }
  );
  const view = parseDeliveryView(new URLSearchParams(next));
  assert.equal(view.page, 0);
  assert.equal(view.focusTask, null);
  assert.equal(view.ownerFilter, "mine");
  assert.equal(view.filter, "ready");
});

test("defaults produce a clean URL and history entries can restore prior views", () => {
  const first = "?page=2&filter=qa_incomplete&task=abc";
  const second = deliveryViewQuery(first, { page: "3", task: null });
  assert.equal(parseDeliveryView(new URLSearchParams(second)).page, 2);
  assert.equal(parseDeliveryView(new URLSearchParams(first)).focusTask, "abc");
  assert.equal(deliveryViewQuery(second, { page: "1", filter: "all" }), "");
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

test("default shows the complete inventory", () => {
  assert.equal(parseDeliveryView(new URLSearchParams()).filter, "all");
  const inventory = deliveryViewQuery("", { filter: "all" });
  assert.equal(inventory, "");
  assert.equal(parseDeliveryView(new URLSearchParams(inventory)).filter, "all");
});

test("named owners and review panels survive shared links", () => {
  const query = deliveryViewQuery("?task=abc", {
    owner: "user_someone_else",
    panels: "history,all-versions,version-v1,finding-f1,!decisions",
  });
  const params = new URLSearchParams(query);
  assert.equal(parseDeliveryView(params).ownerFilter, "user_someone_else");
  assert.equal(
    params.get("panels"),
    "history,all-versions,version-v1,finding-f1,!decisions"
  );
  assert.equal(parseDeliveryView(params).focusTask, "abc");
});

test("changing one view field preserves every other URL parameter", () => {
  const current =
    "?page=2&per_page=50&filter=needs_work&issue=verifier&owner=user_42&group=owner&task=task_7&panels=history%2Call-versions&source=slack";
  const params = new URLSearchParams(deliveryViewQuery(current, { page: "3" }));

  assert.deepEqual(Object.fromEntries(params), {
    page: "3",
    per_page: "50",
    filter: "needs_work",
    issue: "verifier",
    owner: "user_42",
    group: "owner",
    task: "task_7",
    panels: "history,all-versions",
    source: "slack",
  });
});

test("legacy blocked links retain their filter when changing pages", () => {
  const query = deliveryViewQuery("?filter=blocked&owner=mine", { page: "2" });
  const view = parseDeliveryView(new URLSearchParams(query));
  assert.equal(view.filter, "blocked");
  assert.equal(view.ownerFilter, "mine");
  assert.equal(view.page, 1);
});

test("page request keys retain agent filters but exclude disclosures and unrelated parameters", () => {
  const shared = new URLSearchParams(
    "page=2&per_page=50&filter=blocked&issue=verifier&owner=mine&group=owner&task=legacy-task&panels=history&source=agent"
  );
  const key = deliveryPageQuery(shared);
  assert.equal(
    key,
    "?page=2&per_page=50&filter=blocked&issue=verifier&owner=mine&group=owner&task=legacy-task"
  );
  shared.set("panels", "history,all-versions");
  shared.set("source", "another-agent");
  assert.equal(deliveryPageQuery(shared), key);
  assert.equal(
    deliveryPageQuery(
      new URLSearchParams("filter=all&page=1&per_page=25&group=none")
    ),
    ""
  );
});

test("expansion and collapse reuse the visible page, but paging and filters do not", () => {
  const page = {
    tasks: [
      { task_id: "a", task_name: "Task A" },
      { task_id: "b", task_name: "Task B" },
    ],
    member_task_ids: ["a", "b", "c"],
    page: 1,
    per_page: 25,
    total: 60,
    focus_task_id: null,
    focus_outside_filters: false,
  } as import("../src/lib/types").DeliveryPageResponse;
  for (const query of ["", "?task=a", "?task=b", "?task=Task+A"]) {
    assert.equal(deliveryPageContainsView(page, "", query), true);
  }
  for (const query of [
    "?task=c",
    "?page=2",
    "?owner=mine",
    "?filter=ready",
    "?group=owner",
    "?issue=verifier",
    "?per_page=10",
  ]) {
    assert.equal(deliveryPageContainsView(page, "", query), false);
  }
  assert.equal(deliveryPageContainsView(page, "?task=a", "?task=b"), true);
});

test("cached off-filter focus cannot leave its extra row in a different view", () => {
  const page = {
    tasks: [
      { task_id: "a", task_name: "Task A" },
      { task_id: "b", task_name: "Task B" },
    ],
    member_task_ids: ["a", "b"],
    page: 1,
    per_page: 25,
    total: 2,
    focus_task_id: "a",
    focus_outside_filters: true,
  } as import("../src/lib/types").DeliveryPageResponse;
  assert.equal(
    deliveryPageContainsView(
      page,
      "?filter=ready&task=a",
      "?filter=ready&task=Task+A"
    ),
    true
  );
  for (const query of ["?filter=ready", "?filter=ready&task=b"]) {
    assert.equal(
      deliveryPageContainsView(page, "?filter=ready&task=a", query),
      false
    );
  }
});

test("off-page task IDs win over visible legacy names and clamped pages are reusable", () => {
  const page = {
    tasks: [{ task_id: "a", task_name: "b" }],
    member_task_ids: ["a", "b"],
    page: 3,
    per_page: 25,
    total: 60,
    focus_task_id: null,
    focus_outside_filters: false,
  } as import("../src/lib/types").DeliveryPageResponse;
  assert.equal(deliveryPageContainsView(page, "?page=3", "?task=b"), false);
  assert.equal(deliveryPageContainsView(page, "?page=3", "?page=100"), true);
  assert.equal(deliveryPageContainsView(page, "?page=3", "?page=1"), false);
});
