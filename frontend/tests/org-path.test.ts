import assert from "node:assert/strict";
import test from "node:test";

import {
  APP_ROOT_SEGMENTS,
  ORG_SYNC_PATTERNS,
  PUBLIC_ROOT_SEGMENTS,
  parseOrgSlug,
  resolveOrgRequest,
  stripOrgSlug,
  withOrgSlug,
} from "../src/lib/org-path.ts";

test("app and public first segments do not overlap", () => {
  const overlap = APP_ROOT_SEGMENTS.filter((segment) =>
    (PUBLIC_ROOT_SEGMENTS as readonly string[]).includes(segment),
  );
  assert.deepEqual(overlap, []);
});

test("parseOrgSlug reads a leading workspace slug", () => {
  assert.equal(parseOrgSlug("/acme/tasks"), "acme");
  assert.equal(parseOrgSlug("/acme/tasks/task-1/probe"), "acme");
  assert.equal(parseOrgSlug("/acme"), "acme");
  assert.equal(parseOrgSlug("/personal-user_abc/dashboard"), "personal-user_abc");
});

test("parseOrgSlug ignores unprefixed app and public paths", () => {
  assert.equal(parseOrgSlug("/tasks"), null);
  assert.equal(parseOrgSlug("/tasks/task-1"), null);
  assert.equal(parseOrgSlug("/experiments/exp-1"), null);
  assert.equal(parseOrgSlug("/share/token"), null);
  assert.equal(parseOrgSlug("/datasets/token"), null);
  assert.equal(parseOrgSlug("/api/tasks"), null);
  assert.equal(parseOrgSlug("/sign-in"), null);
  assert.equal(parseOrgSlug("/"), null);
});

test("an org cannot collide with a reserved first segment", () => {
  assert.equal(parseOrgSlug("/tasks/dashboard"), null);
  assert.equal(parseOrgSlug("/share/tasks"), null);
  assert.equal(parseOrgSlug("/api/dashboard"), null);
});

test("stripOrgSlug returns the in-app path", () => {
  assert.equal(stripOrgSlug("/acme/tasks/task-1"), "/tasks/task-1");
  assert.equal(stripOrgSlug("/acme"), "/");
  assert.equal(stripOrgSlug("/tasks/task-1"), "/tasks/task-1");
  assert.equal(stripOrgSlug("/share/token"), "/share/token");
});

test("withOrgSlug prefixes app paths and is idempotent", () => {
  assert.equal(withOrgSlug("/tasks", "acme"), "/acme/tasks");
  assert.equal(withOrgSlug("/tasks/task-1?trial=2#step-3", "acme"), "/acme/tasks/task-1?trial=2#step-3");
  assert.equal(withOrgSlug("/acme/tasks", "acme"), "/acme/tasks");
  assert.equal(withOrgSlug("/acme/tasks/task-1", "beta"), "/beta/tasks/task-1");
  assert.equal(withOrgSlug("/dashboard", "acme"), "/acme/dashboard");
});

test("withOrgSlug leaves public, external, and unknown hrefs alone", () => {
  assert.equal(withOrgSlug("/share/token", "acme"), "/share/token");
  assert.equal(withOrgSlug("/datasets/x", "acme"), "/datasets/x");
  assert.equal(withOrgSlug("/api/tasks", "acme"), "/api/tasks");
  assert.equal(withOrgSlug("https://oddish.app/tasks", "acme"), "https://oddish.app/tasks");
  assert.equal(withOrgSlug("/", "acme"), "/");
  assert.equal(withOrgSlug("/tasks", null), "/tasks");
  assert.equal(withOrgSlug("/tasks", undefined), "/tasks");
});

test("signed-in users with an org are redirected onto slugged app URLs", () => {
  assert.deepEqual(
    resolveOrgRequest({
      pathname: "/tasks/task-1",
      userId: "user_1",
      orgSlug: "acme",
    }),
    { action: "redirect", pathname: "/acme/tasks/task-1", status: 308 },
  );
  assert.deepEqual(
    resolveOrgRequest({
      pathname: "/experiments/exp-1",
      userId: "user_1",
      orgSlug: "acme",
    }),
    { action: "redirect", pathname: "/acme/experiments/exp-1", status: 308 },
  );
  assert.deepEqual(
    resolveOrgRequest({
      pathname: "/",
      userId: "user_1",
      orgSlug: "acme",
    }),
    { action: "redirect", pathname: "/acme/dashboard", status: 307 },
  );
});

test("signed-out visitors keep unprefixed experiment URLs for unfurls", () => {
  assert.deepEqual(
    resolveOrgRequest({
      pathname: "/experiments/exp-1",
      userId: null,
      orgSlug: null,
    }),
    { action: "next" },
  );
});

test("slugged app URLs rewrite onto the existing page tree", () => {
  assert.deepEqual(
    resolveOrgRequest({
      pathname: "/acme/tasks/task-1",
      userId: "user_1",
      orgSlug: "acme",
    }),
    { action: "rewrite", pathname: "/tasks/task-1" },
  );
  assert.deepEqual(
    resolveOrgRequest({
      pathname: "/acme/admin/users/u1",
      userId: "user_1",
      orgSlug: "acme",
    }),
    { action: "rewrite", pathname: "/admin/users/u1" },
  );
  assert.deepEqual(
    resolveOrgRequest({
      pathname: "/acme",
      userId: "user_1",
      orgSlug: "acme",
    }),
    { action: "redirect", pathname: "/acme/dashboard", status: 307 },
  );
});

test("public roots and APIs are never rewritten or prefixed", () => {
  for (const pathname of [
    "/share/token",
    "/datasets/token",
    "/sign-in",
    "/sign-up",
    "/api/tasks",
    "/api/public/experiments/x",
  ]) {
    assert.deepEqual(
      resolveOrgRequest({ pathname, userId: "user_1", orgSlug: "acme" }),
      { action: "next" },
    );
  }
});

test("signed-in users without an org keep today's unprefixed app URLs", () => {
  assert.deepEqual(
    resolveOrgRequest({
      pathname: "/dashboard",
      userId: "user_1",
      orgSlug: null,
    }),
    { action: "next" },
  );
  assert.deepEqual(
    resolveOrgRequest({
      pathname: "/",
      userId: "user_1",
      orgSlug: null,
    }),
    { action: "redirect", pathname: "/dashboard", status: 307 },
  );
});

test("Clerk sync patterns cover every app root and skip public roots", () => {
  for (const segment of APP_ROOT_SEGMENTS) {
    assert.ok(ORG_SYNC_PATTERNS.includes(`/:slug/${segment}`));
    assert.ok(ORG_SYNC_PATTERNS.includes(`/:slug/${segment}/(.*)`));
  }
  const joined = ORG_SYNC_PATTERNS.join("\n");
  for (const segment of PUBLIC_ROOT_SEGMENTS) {
    assert.equal(joined.includes(`/${segment}`), false);
  }
});
