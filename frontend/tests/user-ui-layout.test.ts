import assert from "node:assert/strict";
import test from "node:test";
import { setImmediate } from "node:timers/promises";
import {
  DEFAULT_TRIAL_DRAWER_LAYOUT as defaults,
  parseTrialDrawerLayout,
  UserUiLayoutStore,
} from "../src/lib/user-ui-layout.ts";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status });
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

test("a slow read preserves gestures and the other saved fields", async () => {
  const read = deferred<Response>();
  const writes: unknown[] = [];
  const store = new UserUiLayoutStore(async (_url, init) => {
    if (init?.method !== "PUT") return read.promise;
    writes.push(JSON.parse(String(init.body)));
    return json({});
  });
  const stop = store.start();
  store.update({ preferredWidthPx: 950 });
  read.resolve(
    json({ ...defaults, preferredWidthPx: 1200, taskPanePercent: 65 })
  );
  await setImmediate();
  assert.deepEqual(writes, [
    { ...defaults, preferredWidthPx: 950, taskPanePercent: 65 },
  ]);
  stop();
});

test("queued saves are ordered and an old response cannot revert a newer drag", async () => {
  const firstSave = deferred<Response>();
  const widths: unknown[] = [];
  const store = new UserUiLayoutStore(async (_url, init) => {
    if (init?.method !== "PUT") return json(defaults);
    widths.push(JSON.parse(String(init.body)).preferredWidthPx);
    return widths.length === 1 ? firstSave.promise : json({});
  });
  const stop = store.start();
  await setImmediate();
  store.update({ preferredWidthPx: 900 });
  const saving = store.flush();
  store.update({ preferredWidthPx: 1300 });
  await store.flush();
  assert.deepEqual(widths, [900]);
  firstSave.resolve(json({ ...defaults, preferredWidthPx: 900 }));
  await saving;
  await setImmediate();
  assert.deepEqual(widths, [900, 1300]);
  assert.equal(store.getSnapshot().layout.preferredWidthPx, 1300);
  stop();
});

test("failed reads do not save defaults; Retry merges edits into the saved layout", async () => {
  let fail = true;
  const writes: unknown[] = [];
  const store = new UserUiLayoutStore(async (_url, init) => {
    if (init?.method !== "PUT")
      return fail ? json({}, 503) : json({ ...defaults, taskPanePercent: 60 });
    writes.push(JSON.parse(String(init.body)));
    return json({});
  });
  const stop = store.start();
  await setImmediate();
  store.update({ maximized: true });
  await store.flush();
  assert.equal(store.getSnapshot().status, "error");
  assert.deepEqual(writes, []);
  fail = false;
  store.retry();
  await setImmediate();
  assert.deepEqual(writes, [
    { ...defaults, maximized: true, taskPanePercent: 60 },
  ]);
  stop();
});

test("switching accounts discards pending work and ignores the old read", async () => {
  const oldRead = deferred<Response>();
  let oldWrites = 0;
  const old = new UserUiLayoutStore(async (_url, init) => {
    if (init?.method === "PUT") oldWrites++;
    return oldRead.promise;
  });
  const stopOld = old.start();
  old.update({ preferredWidthPx: 1000 });
  stopOld();
  const next = new UserUiLayoutStore(async () =>
    json({ ...defaults, preferredWidthPx: 700 })
  );
  const stopNext = next.start();
  oldRead.resolve(json({ ...defaults, preferredWidthPx: 1400 }));
  await setImmediate();
  await old.flush();
  assert.equal(oldWrites, 0);
  assert.equal(next.getSnapshot().layout.preferredWidthPx, 700);
  stopNext();
});

test("edits during load cannot restore two hidden panes", async () => {
  const read = deferred<Response>();
  const store = new UserUiLayoutStore(async (_url, init) =>
    init?.method === "PUT" ? json({}) : read.promise
  );
  const stop = store.start();
  store.update({ showTrial: false });
  read.resolve(json({ ...defaults, showTask: false }));
  await setImmediate();
  assert.equal(store.getSnapshot().layout.showTask, true);
  assert.equal(store.getSnapshot().layout.showTrial, false);
  stop();
});

test("public layouts work in memory and malformed server values are rejected", () => {
  const store = new UserUiLayoutStore(null);
  const stop = store.start();
  store.update({ taskPanePercent: 60 });
  assert.equal(store.getSnapshot().layout.taskPanePercent, 60);
  assert.equal(store.getSnapshot().status, "ready");
  for (const value of [
    null,
    { ...defaults, version: 2 },
    { ...defaults, preferredWidthPx: NaN },
    { ...defaults, showTask: false, showTrial: false },
  ]) {
    assert.throws(() => parseTrialDrawerLayout(value));
  }
  assert.equal(
    parseTrialDrawerLayout({ ...defaults, taskPanePercent: 0 }).taskPanePercent,
    15
  );
  stop();
});
