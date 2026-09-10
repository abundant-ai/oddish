import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import * as React from "react";
import * as jsx from "react/jsx-runtime";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";
import type { Task } from "../src/lib/types.ts";

const cache: Record<string, unknown> = {};
function load(name: string): unknown {
  if (name in cache) return cache[name];
  assert.ok(name.startsWith("@/lib/"), name);
  const exports = {};
  cache[name] = exports;
  runInNewContext(
    ts.transpileModule(
      readFileSync(
        new URL(`../src/${name.slice(2)}.ts`, import.meta.url),
        "utf8"
      ),
      { compilerOptions: { module: ts.ModuleKind.CommonJS } }
    ).outputText,
    { exports, require: load }
  );
  return exports;
}
const review = load("@/lib/review") as typeof import("../src/lib/review.ts");
const task = {
  id: "task-1",
  name: "Task",
  status: "completed",
  verdict_status: "success",
  review_version_matches: true,
} as Task;

// Render the production chip, isolating only its surrounding table and tooltip UI.
const source = ts.createSourceFile(
  "table.tsx",
  readFileSync(
    new URL("../src/components/experiment-trials-table.tsx", import.meta.url),
    "utf8"
  ),
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.TSX
);
const chip = source.statements.find(
  (node) =>
    ts.isFunctionDeclaration(node) && node.name?.text === "TaskVerdictChip"
);
assert.ok(chip);
const box = ({ children }: { children?: React.ReactNode }) =>
  React.createElement("div", null, children);
const exports: Record<
  string,
  React.ComponentType<{ task: Task; ungradedSettled: number }>
> = {};
runInNewContext(
  ts.transpileModule(
    `${chip.getText(source)}\nexports.Chip = TaskVerdictChip;`,
    {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        jsx: ts.JsxEmit.ReactJSX,
      },
    }
  ).outputText,
  {
    exports,
    require: (name: string) => {
      assert.equal(name, "react/jsx-runtime");
      return jsx;
    },
    ...review,
    Tooltip: box,
    TooltipTrigger: box,
    TooltipContent: box,
  }
);

for (const [label, is_good, expected] of [
  ["accept", null, "accepted"],
  ["reject", null, "needs_fixes"],
  [undefined, true, "accepted"],
  [undefined, false, "needs_fixes"],
  ["accept", false, "accepted"],
  ["reject", true, "needs_fixes"],
] as const) {
  test(`published ${label ?? is_good} verdict is ${expected}`, () => {
    const reviewed = {
      ...task,
      verdict: { verdict: label, is_good, confidence: null },
    };
    assert.equal(review.taskReviewStatus(reviewed), expected);
    assert.equal(
      review.taskReviewStatus({ ...reviewed, analysis_status: "running" }),
      expected
    );
    assert.equal(
      review.taskReviewStatus({ ...reviewed, review_version_matches: false }),
      "outdated"
    );
    assert.equal(
      review.taskReviewStatus({ ...reviewed, verdict_status: "failed" }),
      "error"
    );
    assert.equal(
      review.taskReviewStatus({ ...reviewed, verdict_status: "queued" }),
      "queued"
    );
    assert.equal(
      review.taskReviewStatus({ ...reviewed, verdict_status: "running" }),
      "running"
    );
    for (const ungradedSettled of [0, 2]) {
      const html = renderToStaticMarkup(
        React.createElement(exports.Chip, { task: reviewed, ungradedSettled })
      );
      assert.ok(html.includes(review.REVIEW_LABELS[expected]), html);
      assert.ok(
        html.includes(
          expected === "accepted" ? "bg-emerald-100" : "bg-red-100"
        ),
        html
      );
      assert.ok(!html.includes("Review outdated"), html);
      assert.equal(html.includes("2 settled trials"), ungradedSettled === 2);
    }
    const outdated = renderToStaticMarkup(
      React.createElement(exports.Chip, {
        task: { ...reviewed, review_version_matches: false },
        ungradedSettled: 0,
      })
    );
    assert.ok(outdated.includes("Review outdated"), outdated);
  });
}

test("missing and inconclusive verdicts remain unreviewed", () => {
  assert.equal(review.taskReviewStatus(task), "never");
  assert.equal(
    review.taskReviewStatus({
      ...task,
      verdict: { is_good: null, confidence: null },
    }),
    "never"
  );
});
