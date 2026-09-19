import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";

// Exercise the production event handlers without mounting a browser or mocking
// the drawer's visual children. TypeScript locates the named handler bodies.
function handler(file: string, name: string, context: Record<string, unknown>) {
  const source = ts.createSourceFile(
    file,
    readFileSync(new URL(`../src/${file}`, import.meta.url), "utf8"),
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX
  );
  let initializer: ts.Expression | undefined;
  function visit(node: ts.Node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(source) === name) {
      initializer = node.initializer;
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
  assert.ok(initializer, `${name} exists in ${file}`);
  return runInNewContext(
    ts.transpileModule(`(${initializer.getText(source)})`, {
      compilerOptions: { target: ts.ScriptTarget.ES2022 },
    }).outputText,
    { Error, ...context }
  );
}

const previous = { id: "task-1-0", status: "failed", reward: 0 };
const replacement = { id: "task-1-1", status: "queued", reward: null };

for (const [page, file, setter, field] of [
  [
    "task",
    "app/(app)/tasks/[task_id]/task-detail-client.tsx",
    "setDrawer",
    "fallbackTrial",
  ],
  [
    "experiment",
    "components/experiment-detail-view.tsx",
    "setDrawerState",
    "trial",
  ],
] as const) {
  for (const selection of [
    "original",
    "closed",
    "other trial",
    "task overview",
  ]) {
    test(`${page}: retry completes while viewing ${selection}`, async () => {
      let drawer: Record<string, unknown> | null = {
        mode: "trial",
        [field]: previous,
        trialIndex: 0,
        task: { id: "task-1" },
      };
      const onRetried = handler(file, "handleTrialRetried", {
        [setter]: (update: (current: typeof drawer) => typeof drawer) => {
          drawer = update(drawer);
        },
      });
      const events: string[] = [];
      let finishRead!: (value: typeof replacement) => void;
      const read = new Promise<typeof replacement>((resolve) => {
        finishRead = resolve;
      });
      const retry = handler(
        "components/trial-detail-panel.tsx",
        "handleRetry",
        {
          trial: previous,
          task: { id: "task-1" },
          retrying: false,
          canRetry: true,
          apiBaseUrl: "/api",
          onRetried,
          setRetrying: (busy: boolean) => events.push(`busy:${busy}`),
          setRetryError: (error: unknown) => assert.equal(error, null),
          fetch: async (url: string, options: { method: string }) => {
            assert.equal(url, "/api/trials/task-1-0/retry");
            assert.equal(options.method, "POST");
            return {
              ok: true,
              json: async () => ({ trial_id: replacement.id }),
            };
          },
          preloadTrial: (base: string, id: string) => {
            assert.equal(base, "/api");
            assert.equal(id, replacement.id);
            return read;
          },
          onRetry: async (ids: string[]) => {
            assert.equal(ids[0], "task-1");
            events.push("refresh");
          },
          onClose: () => assert.fail("Retry must not close the drawer"),
        }
      );
      const pending = retry();
      if (selection === "closed") drawer = null;
      if (selection === "other trial")
        drawer = { ...drawer, [field]: { id: "task-1-9" } };
      if (selection === "task overview") drawer = { ...drawer, mode: "task" };
      const beforeCompletion = drawer;
      finishRead(replacement);
      await pending;
      if (selection === "original") {
        assert.equal(drawer?.[field], replacement);
        assert.equal(drawer?.mode, "trial");
        assert.equal(drawer?.task, beforeCompletion?.task);
        if (page === "experiment") assert.equal(drawer?.trialIndex, null);
      } else {
        assert.equal(
          drawer,
          beforeCompletion,
          "A late retry preserves the user's selection"
        );
      }
      assert.deepEqual(events, ["busy:true", "refresh", "busy:false"]);
    });
  }
}

test("a rejected retry displays the server error without changing the drawer", async () => {
  const errors: unknown[] = [];
  const retry = handler("components/trial-detail-panel.tsx", "handleRetry", {
    trial: previous,
    retrying: false,
    canRetry: true,
    apiBaseUrl: "/api",
    setRetrying: () => {},
    setRetryError: (error: unknown) => errors.push(error),
    fetch: async () => ({
      ok: false,
      json: async () => ({ detail: "Quota exceeded" }),
    }),
    preloadTrial: () => assert.fail("Rejected retries have no replacement"),
    onRetried: () =>
      assert.fail("Rejected retries must keep the selected trial"),
    onRetry: () => assert.fail("Rejected retries do not refresh"),
    onClose: () => assert.fail("Rejected retries must not close the drawer"),
  });
  await retry();
  assert.deepEqual(errors, [null, "Quota exceeded"]);
});

for (const [failure, expectedError, json, preloadTrial] of [
  [
    "response cannot be decoded",
    "Invalid retry response",
    async () => {
      throw new Error("Invalid retry response");
    },
    () => assert.fail("An undecodable response has no replacement ID"),
  ],
  [
    "replacement cannot be loaded",
    "Replacement unavailable",
    async () => ({ trial_id: replacement.id }),
    async () => {
      throw new Error("Replacement unavailable");
    },
  ],
] as const) {
  test(`a successful retry refreshes and closes when the ${failure}`, async () => {
    const events: string[] = [];
    const errors: unknown[] = [];
    const retry = handler("components/trial-detail-panel.tsx", "handleRetry", {
      trial: previous,
      task: { id: "task-1" },
      retrying: false,
      canRetry: true,
      apiBaseUrl: "/api",
      setRetrying: (busy: boolean) => events.push(`busy:${busy}`),
      setRetryError: (error: unknown) => errors.push(error),
      fetch: async () => ({ ok: true, json }),
      preloadTrial,
      onRetried: () =>
        assert.fail("The drawer cannot select an unavailable replacement"),
      onRetry: async (ids: string[]) => {
        assert.equal(ids[0], "task-1");
        events.push("refresh");
      },
      onClose: () => events.push("close"),
    });

    await retry();

    assert.deepEqual(events, ["busy:true", "close", "refresh", "busy:false"]);
    assert.deepEqual(errors, [null, expectedError]);
  });
}
