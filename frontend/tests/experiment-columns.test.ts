import assert from "node:assert/strict";
import test from "node:test";
import {
  MAX_REMEMBERED_EXPERIMENTS,
  loadHiddenAgents,
  readHiddenAgents,
  saveHiddenAgents,
  writeHiddenAgents,
} from "../src/lib/experiment-columns.ts";

test("a hidden column comes back hidden when the experiment is reopened", () => {
  const stored = writeHiddenAgents(null, "exp_1", ["codex"]);

  assert.deepEqual(readHiddenAgents(stored, "exp_1"), ["codex"]);
});

test("hiding a column in one experiment leaves other experiments alone", () => {
  const stored = writeHiddenAgents(null, "exp_1", ["codex"]);

  assert.deepEqual(readHiddenAgents(stored, "exp_2"), []);
});

test("showing every column again drops the experiment from storage", () => {
  const hidden = writeHiddenAgents(null, "exp_1", ["codex"]);
  const cleared = writeHiddenAgents(hidden, "exp_1", []);

  assert.deepEqual(readHiddenAgents(cleared, "exp_1"), []);
  assert.equal(cleared, "[]", "an empty selection should not be stored");
});

test("an unseen experiment hides nothing", () => {
  assert.deepEqual(readHiddenAgents(null, "exp_1"), []);
  assert.deepEqual(readHiddenAgents("[]", "exp_1"), []);
});

test("corrupt storage hides nothing rather than blanking the table", () => {
  for (const raw of ["not json", "{}", '"codex"', "null", "42", "[1,2,3]"]) {
    assert.deepEqual(
      readHiddenAgents(raw, "exp_1"),
      [],
      `expected no hidden columns for ${raw}`
    );
  }
});

test("only the most recent experiments are kept", () => {
  let stored: string | null = null;
  for (let i = 0; i < MAX_REMEMBERED_EXPERIMENTS + 10; i += 1) {
    stored = writeHiddenAgents(stored, `exp_${i}`, ["codex"]);
  }

  assert.equal(JSON.parse(stored as string).length, MAX_REMEMBERED_EXPERIMENTS);
  assert.deepEqual(readHiddenAgents(stored, "exp_0"), []);
  assert.deepEqual(
    readHiddenAgents(stored, `exp_${MAX_REMEMBERED_EXPERIMENTS + 9}`),
    ["codex"]
  );
});

test("revisiting an experiment refreshes it rather than duplicating it", () => {
  let stored = writeHiddenAgents(null, "exp_1", ["codex"]);
  stored = writeHiddenAgents(stored, "exp_2", ["nop"]);
  stored = writeHiddenAgents(stored, "exp_1", ["oracle"]);

  assert.equal(JSON.parse(stored).length, 2);
  assert.deepEqual(readHiddenAgents(stored, "exp_1"), ["oracle"]);
  assert.deepEqual(readHiddenAgents(stored, "exp_2"), ["nop"]);
});

function withStorage(storage: unknown, run: () => void): void {
  const previous = (globalThis as { window?: unknown }).window;
  (globalThis as { window?: unknown }).window = { localStorage: storage };
  try {
    run();
  } finally {
    if (previous === undefined)
      delete (globalThis as { window?: unknown }).window;
    else (globalThis as { window?: unknown }).window = previous;
  }
}

const throwingStorage = {
  getItem() {
    throw new DOMException("denied", "SecurityError");
  },
  setItem() {
    throw new DOMException("denied", "SecurityError");
  },
};

function memoryStorage(seed: string | null = null) {
  let value = seed;
  return {
    getItem: () => value,
    setItem: (_key: string, next: string) => {
      value = next;
    },
    get value() {
      return value;
    },
  };
}

test("blocked storage hides nothing instead of throwing", () => {
  withStorage(throwingStorage, () => {
    assert.deepEqual(loadHiddenAgents("exp_1"), []);
  });
});

test("a full or blocked store drops the write instead of throwing", () => {
  withStorage(throwingStorage, () => {
    assert.doesNotThrow(() => saveHiddenAgents("exp_1", ["codex"]));
  });

  const quotaBound = {
    getItem: () => "[]",
    setItem() {
      throw new DOMException("full", "QuotaExceededError");
    },
  };
  withStorage(quotaBound, () => {
    assert.doesNotThrow(() => saveHiddenAgents("exp_1", ["codex"]));
  });
});

test("working storage still round-trips through the guards", () => {
  const store = memoryStorage();
  withStorage(store, () => {
    saveHiddenAgents("exp_1", ["codex"]);
    assert.deepEqual(loadHiddenAgents("exp_1"), ["codex"]);
    assert.deepEqual(loadHiddenAgents("exp_2"), []);
  });
});

test("server rendering reads nothing and writes nothing", () => {
  const previous = (globalThis as { window?: unknown }).window;
  delete (globalThis as { window?: unknown }).window;
  try {
    assert.deepEqual(loadHiddenAgents("exp_1"), []);
    assert.doesNotThrow(() => saveHiddenAgents("exp_1", ["codex"]));
  } finally {
    if (previous !== undefined)
      (globalThis as { window?: unknown }).window = previous;
  }
});
