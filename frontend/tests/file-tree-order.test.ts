import { test } from "node:test";
import assert from "node:assert/strict";
import { FileTree } from "@pierre/trees";

// Relative import on purpose: these tests run under plain `node --test`
// (see the `test` script), which strips types but does not resolve the
// `@/` tsconfig alias.
import { firstFilePath } from "../src/lib/file-tree-order.ts";

// The one contract callers rely on: firstFilePath returns the file the tree
// renders as its top row (null when there are none). Each case asserts that
// against a real model built with FileTreePane's options, so a library
// upgrade that changes sort or flattening semantics fails here instead of
// silently mis-selecting. The path sets are the shapes that order
// differently under different comparators.
const CASES: Array<[string, string[]]> = [
  ["empty listing", []],
  ["single file", ["result.json"]],
  ["file vs directory", ["aaa.txt", "zzz/nested.txt"]],
  ["dotfiles", ["apps/main.ts", ".hidden/config.toml"]],
  ["mixed case", ["Zebra/a.txt", "apple/b.txt"]],
  ["numeric segments", ["attempt_10/r.json", "attempt_2/r.json"]],
  [
    "realistic artifact listing",
    [
      "result.json",
      "logs/agent.log",
      "logs/nested/deep/trace.txt",
      ".hidden/config.toml",
      "setup/README.md",
      "steps/step_10/out.txt",
      "steps/step_2/out.txt",
    ],
  ],
];

for (const [name, paths] of CASES) {
  test(`matches the rendered top file row: ${name}`, () => {
    const tree = new FileTree({
      paths,
      flattenEmptyDirectories: true,
      initialExpansion: "open",
    });
    const rows = tree.getVisibleRows(0, tree.getVisibleCount());
    const topFileRow = rows.find((row) => row.kind === "file")?.path ?? null;
    assert.equal(firstFilePath(paths), topFileRow);
  });
}

// Concrete anchors, so a bug that breaks both sides of the parity check the
// same way can't pass silently. The numeric case is a regression test: a
// plain string compare selects attempt_10.
test("natural sort selects attempt_2 over attempt_10", () => {
  assert.equal(
    firstFilePath(["attempt_10/r.json", "attempt_2/r.json"]),
    "attempt_2/r.json",
  );
});

test("empty listing has no first file", () => {
  assert.equal(firstFilePath([]), null);
});
