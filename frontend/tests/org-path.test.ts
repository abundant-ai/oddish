import assert from "node:assert/strict";
import test from "node:test";

import {
  APP_ROOT_SEGMENTS,
  ORG_PREFIX,
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
  assert.equal(
    (APP_ROOT_SEGMENTS as readonly string[]).includes(ORG_PREFIX),
    false,
  );
});

test("parseOrgSlug reads /orgs/{slug}/…", () => {
  assert.equal(parseOrgSlug("/orgs/acme/tasks"), "acme");
  assert.equal(parseOrgSlug("/orgs/acme/tasks/task-1/probe"), "acme");
  assert.equal(parseOrgSlug("/orgs/acme"), "acme");
  assert.equal(
    parseOrgSlug("/orgs/personal-user_abc/dashboard"),
    "personal-user_abc",
  );
  assert.equal(parseOrgSlug("/orgs/tasks/dashboard"), "tasks");
});

test("parseOrgSlug ignores unprefixed app, public, and legacy first-segment slugs", () => {
  assert.equal(parseOrgSlug("/tasks"), null);
  assert.equal(parseOrgSlug("/tasks/task-1"), null);
  assert.equal(parseOrgSlug("/experiments/exp-1"), null);
  assert.equal(parseOrgSlug("/share/token"), null);
  assert.equal(parseOrgSlug("/datasets/token"), null);
  assert.equal(parseOrgSlug("/api/tasks"), null);
  assert.equal(parseOrgSlug("/sign-in"), null);
  assert.equal(parseOrgSlug("/"), null);
  assert.equal(parseOrgSlug("/acme/tasks"), null);
  assert.equal(parseOrgSlug("/orgs"), null);
});

test("stripOrgSlug returns the in-app path", () => {
  assert.equal(stripOrgSlug("/orgs/acme/tasks/task-1"), "/tasks/task-1");
  assert.equal(stripOrgSlug("/orgs/acme"), "/");
  assert.equal(stripOrgSlug("/tasks/task-1"), "/tasks/task-1");
  assert.equal(stripOrgSlug("/share/token"), "/share/token");
});

test("withOrgSlug prefixes app paths under /orgs/{slug}", () => {
  assert.equal(withOrgSlug("/tasks", "acme"), "/orgs/acme/tasks");
  assert.equal(
    withOrgSlug("/tasks/task-1?trial=2#step-3", "acme"),
    "/orgs/acme/tasks/task-1?trial=2#step-3",
  );
  assert.equal(withOrgSlug("/orgs/acme/tasks", "acme"), "/orgs/acme/tasks");
  assert.equal(
    withOrgSlug("/orgs/acme/tasks/task-1", "beta"),
    "/orgs/beta/tasks/task-1",
  );
  assert.equal(withOrgSlug("/dashboard", "acme"), "/orgs/acme/dashboard");
});

test("withOrgSlug leaves public, external, and unknown hrefs alone", () => {
  assert.equal(withOrgSlug("/share/token", "acme"), "/share/token");
  assert.equal(withOrgSlug("/datasets/x", "acme"), "/datasets/x");
  assert.equal(withOrgSlug("/api/tasks", "acme"), "/api/tasks");
  assert.equal(
    withOrgSlug("https://oddish.app/tasks", "acme"),
    "https://oddish.app/tasks",
  );
  assert.equal(withOrgSlug("/", "acme"), "/");
  assert.equal(withOrgSlug("/tasks", null), "/tasks");
  assert.equal(withOrgSlug("/tasks", undefined), "/tasks");
});

test("signed-in users with an org are redirected onto /orgs/{slug} app URLs", () => {
  assert.deepEqual(
    resolveOrgRequest({
      pathname: "/tasks/task-1",
      userId: "user_1",
      orgSlug: "acme",
    }),
    { action: "redirect", pathname: "/orgs/acme/tasks/task-1", status: 308 },
  );
  assert.deepEqual(
    resolveOrgRequest({
      pathname: "/experiments/exp-1",
      userId: "user_1",
      orgSlug: "acme",
    }),
    {
      action: "redirect",
      pathname: "/orgs/acme/experiments/exp-1",
      status: 308,
    },
  );
  assert.deepEqual(
    resolveOrgRequest({
      pathname: "/",
      userId: "user_1",
      orgSlug: "acme",
    }),
    { action: "redirect", pathname: "/orgs/acme/dashboard", status: 307 },
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
      pathname: "/orgs/acme/tasks/task-1",
      userId: "user_1",
      orgSlug: "acme",
    }),
    { action: "rewrite", pathname: "/tasks/task-1" },
  );
  assert.deepEqual(
    resolveOrgRequest({
      pathname: "/orgs/acme/admin/users/u1",
      userId: "user_1",
      orgSlug: "acme",
    }),
    { action: "rewrite", pathname: "/admin/users/u1" },
  );
  assert.deepEqual(
    resolveOrgRequest({
      pathname: "/orgs/acme",
      userId: "user_1",
      orgSlug: "acme",
    }),
    { action: "redirect", pathname: "/orgs/acme/dashboard", status: 307 },
  );
});

test("legacy /{slug}/… URLs move under /orgs/{slug}", () => {
  assert.deepEqual(
    resolveOrgRequest({
      pathname: "/acme/tasks/task-1",
      userId: "user_1",
      orgSlug: "acme",
    }),
    { action: "redirect", pathname: "/orgs/acme/tasks/task-1", status: 308 },
  );
  assert.deepEqual(
    resolveOrgRequest({
      pathname: "/acme",
      userId: "user_1",
      orgSlug: "acme",
    }),
    { action: "redirect", pathname: "/orgs/acme/dashboard", status: 308 },
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

test("Clerk sync patterns only match /orgs/{slug}", () => {
  assert.deepEqual(ORG_SYNC_PATTERNS, [
    "/orgs/:slug",
    "/orgs/:slug/(.*)",
  ]);
});
