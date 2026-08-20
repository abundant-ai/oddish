import { test } from "node:test";
import assert from "node:assert/strict";
import { FileTree } from "@pierre/trees";

// Relative import on purpose: these tests run under plain `node --test`
// (see the `test` script), which strips types but does not resolve the
// `@/` tsconfig alias.
import { firstFilePath } from "../src/lib/file-tree-order.ts";

test("empty listing has no first file", () => {
  assert.equal(firstFilePath([]), null);
});

test("single file", () => {
  assert.equal(firstFilePath(["result.json"]), "result.json");
});

test("directories sort above files at the same level", () => {
  assert.equal(firstFilePath(["aaa.txt", "zzz/nested.txt"]), "zzz/nested.txt");
});

test("dot-prefixed names sort before letters within a level", () => {
  assert.equal(
    firstFilePath(["apps/main.ts", ".hidden/config.toml"]),
    ".hidden/config.toml",
  );
});

test("names compare case-insensitively", () => {
  assert.equal(firstFilePath(["Zebra/a.txt", "apple/b.txt"]), "apple/b.txt");
});

test("numeric segments natural-sort", () => {
  // Harbor listings are full of attempt_{n} / step names; a plain string
  // compare would put attempt_10 first.
  assert.equal(
    firstFilePath(["attempt_10/r.json", "attempt_2/r.json"]),
    "attempt_2/r.json",
  );
});

test("matches the first file row of a real tree model", () => {
  // The invariant callers rely on: the default selection is the top file of
  // the rendered tree. Assert it against an actual model, with the same
  // options FileTreePane uses, so a library upgrade that changes sort or
  // flattening semantics fails here instead of silently mis-selecting.
  const paths = [
    "result.json",
    "logs/agent.log",
    "logs/nested/deep/trace.txt",
    ".hidden/config.toml",
    "setup/README.md",
    "steps/step_10/out.txt",
    "steps/step_2/out.txt",
  ];
  const tree = new FileTree({
    paths,
    flattenEmptyDirectories: true,
    initialExpansion: "open",
  });
  const rows = tree.getVisibleRows(0, tree.getVisibleCount());
  const firstFileRow = rows.find((row) => row.kind === "file");
  assert.equal(firstFilePath(paths), firstFileRow?.path);
  assert.equal(firstFilePath(paths), ".hidden/config.toml");
});
