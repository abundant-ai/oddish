import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import * as React from "react";
import * as jsx from "react/jsx-runtime";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";
import type { Task, Trial } from "../src/lib/types.ts";

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
const jobs = load(
  "@/lib/job-status"
) as typeof import("../src/lib/job-status.ts");
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
  React.ComponentType<{
    task: Task;
    ungradedSettled: number;
    onOpen?: () => void;
  }>
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
    ...jobs,
    Tooltip: box,
    TooltipTrigger: box,
    TooltipContent: box,
  }
);

const badgeSource = ts.createSourceFile(
  "badge.tsx",
  readFileSync(
    new URL("../src/components/task-verdict-badge.tsx", import.meta.url),
    "utf8"
  ),
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.TSX
);
const presentation = badgeSource.statements.find(
  (node) =>
    ts.isFunctionDeclaration(node) && node.name?.text === "presentVerdict"
);
assert.ok(presentation);
const badge: {
  present?: (
    task: Task,
    iconSize: string,
    active: boolean
  ) => { title: string; isGood: boolean | null };
} = {};
runInNewContext(
  ts.transpileModule(
    `${presentation.getText(badgeSource)}\nexports.present = presentVerdict;`,
    {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        jsx: ts.JsxEmit.ReactJSX,
      },
    }
  ).outputText,
  {
    exports: badge,
    require: (name: string) => {
      assert.equal(name, "react/jsx-runtime");
      return jsx;
    },
    ...review,
    Loader2: box,
    AlertTriangle: box,
    CheckCircle2: box,
    Microscope: box,
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
      review.taskReviewFilter(reviewed),
      expected === "accepted" ? "accepted" : "rejected"
    );
    const presented = badge.present!(reviewed, "", false);
    assert.equal(presented.title, review.VERDICT_LABELS[expected]);
    assert.equal(presented.isGood, expected === "accepted");
    for (const [override, state] of [
      [{ review_version_matches: false }, "outdated"],
      [{ verdict_status: "failed" }, "error"],
      [{ verdict_status: "queued" }, "queued"],
      [{ verdict_status: "running" }, "running"],
    ] as const) {
      const inactive = badge.present!({ ...reviewed, ...override }, "", false);
      assert.equal(inactive.title, review.VERDICT_LABELS[state]);
      assert.equal(inactive.isGood, null);
      assert.equal(
        review.taskReviewFilter({ ...reviewed, ...override }),
        state === "outdated"
          ? "unreviewed"
          : state === "error"
            ? "failed"
            : "running"
      );
    }
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
      assert.ok(html.includes(review.VERDICT_LABELS[expected]), html);
      assert.ok(
        html.includes(
          expected === "accepted" ? "bg-emerald-100" : "bg-red-100"
        ),
        html
      );
      assert.ok(!html.includes("Verdict outdated"), html);
      assert.equal(html.includes("2 settled trials"), ungradedSettled === 2);
    }
    const outdated = renderToStaticMarkup(
      React.createElement(exports.Chip, {
        task: { ...reviewed, review_version_matches: false },
        ungradedSettled: 0,
      })
    );
    assert.ok(outdated.includes("Verdict outdated"), outdated);
  });
}

test("missing and inconclusive verdicts remain unreviewed", () => {
  assert.equal(review.taskReviewStatus(task), "never");
  assert.equal(review.taskReviewFilter(task), "unreviewed");
  assert.equal(
    review.taskReviewStatus({
      ...task,
      verdict: { is_good: null, confidence: null },
    }),
    "never"
  );
});

test("verdict failure copy does not rename execution-review failure", () => {
  assert.equal(
    badge.present!({ ...task, verdict_status: "failed" }, "", false).title,
    "Verdict could not complete"
  );
  assert.equal(review.REVIEW_LABELS.error, "Review could not complete");
  assert.equal(review.REVIEW_LABELS.accepted, "Accepted");
});

test("source review requires its own status, never a finding-count inference", () => {
  assert.equal(
    review.preTrialReviewLabel({ ...task, must_fix_count: 0 }),
    "Not reviewed"
  );
  assert.equal(
    review.preTrialReviewLabel({
      ...task,
      pre_trial_status: "failed",
      must_fix_count: 0,
    }),
    "Could not complete"
  );
  assert.equal(
    review.preTrialReviewLabel({
      ...task,
      pre_trial_status: "running",
      must_fix_count: 2,
    }),
    "Running"
  );
  assert.equal(
    review.preTrialReviewLabel({ ...task, pre_trial_status: "success" }),
    "Completed"
  );
  assert.equal(
    review.preTrialReviewLabel({
      ...task,
      pre_trial_status: "success",
      must_fix_count: 0,
    }),
    "Passed"
  );
  assert.equal(
    review.preTrialReviewLabel({
      ...task,
      pre_trial_status: "success",
      must_fix_count: 2,
    }),
    "Findings"
  );
});

test("post-trial outcomes preserve passed, failed, incomplete, and remaining review counts", () => {
  const trial = (props: Partial<Trial>) =>
    ({
      agent: "codex",
      status: "failed",
      analysis_status: "success",
      task_version_id: "v1",
      ...props,
    }) as Trial;
  const reviewed = {
    ...task,
    current_version_id: "v2",
    trial_version_id: "v1",
    trials: [
      trial({
        analysis: { classification: "GOOD_FAILURE" } as Trial["analysis"],
      }),
      trial({
        analysis: { classification: "GOOD_SUCCESS" } as Trial["analysis"],
      }),
      trial({
        analysis: { classification: "BAD_SUCCESS" } as Trial["analysis"],
      }),
      trial({
        analysis: { classification: "BAD_FAILURE" } as Trial["analysis"],
      }),
      trial({
        analysis: { classification: "HARNESS_ERROR" } as Trial["analysis"],
      }),
      trial({
        analysis_status: "queued",
        analysis: { classification: "GOOD_SUCCESS" } as Trial["analysis"],
      }),
      trial({
        analysis_status: "running",
        analysis: { classification: "GOOD_SUCCESS" } as Trial["analysis"],
      }),
      trial({
        analysis_status: "failed",
        analysis: { classification: "GOOD_SUCCESS" } as Trial["analysis"],
      }),
      trial({ analysis_status: "success" }),
      trial({ agent: "oracle" }),
      trial({ is_probe: true }),
      trial({ kind: "qa" }),
      trial({ superseded_by_trial_id: "replacement" }),
      trial({ task_version_id: "v2" }),
    ],
  };
  assert.equal(
    review.postTrialReviewLabel(reviewed),
    "2 passed · 2 failed · 2 could not complete · 1 running · 1 pending · 1 unreviewed"
  );
  assert.equal(review.postTrialReviewLabel(task), "No solver reviews loaded");
});

test("post-trial classifications require completed analysis, including legacy versionless rows", () => {
  const trial = {
    agent: "codex",
    status: "success",
    task_version_id: null,
    analysis: { classification: "GOOD_SUCCESS" },
  } as Trial;
  const versionless = {
    ...task,
    trial_version_id: null,
    current_version_id: "v2",
    trials: [trial],
  };
  assert.equal(review.postTrialReviewLabel(versionless), "1 unreviewed");
  assert.equal(
    review.postTrialReviewLabel({
      ...versionless,
      trials: [{ ...trial, analysis_status: "success" }],
    }),
    "1 passed"
  );
});

test("accepted and missing verdict chips have concise exact labels", () => {
  for (const [verdict, label] of [
    [{ verdict: "accept", is_good: true, confidence: null }, "Accepted"],
    [null, "No verdict"],
  ] as const) {
    const html = renderToStaticMarkup(
      React.createElement(exports.Chip, {
        task: { ...task, verdict },
        ungradedSettled: 0,
      })
    );
    assert.ok(html.includes(`>${label}</span>`), html);
    assert.doesNotMatch(html, /Verdict:/);
  }
});

test("rejected verdict is the accessible findings button", () => {
  const html = renderToStaticMarkup(
    React.createElement(exports.Chip, {
      task: {
        ...task,
        name: "Broken task",
        verdict: { verdict: "reject", is_good: false, confidence: null },
      },
      ungradedSettled: 0,
      onOpen: () => {},
    })
  );
  assert.match(html, /<button[^>]*aria-label="Open findings for Broken task"/);
  assert.match(html, /Rejected/);
});

test("rejected verdict displays a single exact must-fix count on its findings button", () => {
  const html = renderToStaticMarkup(
    React.createElement(exports.Chip, {
      task: {
        ...task,
        name: "Broken task",
        must_fix_count: 1,
        verdict: { verdict: "reject", is_good: false, confidence: null },
      },
      ungradedSettled: 0,
      onOpen: () => {},
    })
  );
  assert.match(html, /<button[^>]*aria-label="Open findings for Broken task"/);
  assert.equal((html.match(/1 Must Fix/g) ?? []).length, 1);
  assert.doesNotMatch(html, /Must Fix Finding/);
});

test("verdict summary hides empty categories and keeps clearing an active filter available", () => {
  const file = ts.createSourceFile(
    "detail.tsx",
    readFileSync(
      new URL("../src/components/experiment-detail-view.tsx", import.meta.url),
      "utf8"
    ),
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX
  );
  const summary = file.statements.find(
    (node) =>
      ts.isFunctionDeclaration(node) &&
      node.name?.text === "ExperimentSummaryBar"
  );
  assert.ok(summary);
  const output: { Summary?: React.ComponentType<Record<string, unknown>> } = {};
  runInNewContext(
    ts.transpileModule(
      `${summary.getText(file)}\nexports.Summary = ExperimentSummaryBar;`,
      {
        compilerOptions: {
          module: ts.ModuleKind.CommonJS,
          jsx: ts.JsxEmit.ReactJSX,
        },
      }
    ).outputText,
    {
      exports: output,
      require: () => jsx,
      KpiTile: ({
        label,
        children,
      }: {
        label: string;
        children: React.ReactNode;
      }) => React.createElement("section", null, label, children),
    }
  );
  const html = renderToStaticMarkup(
    React.createElement(output.Summary!, {
      taskCount: 3,
      summary: {
        completedTrials: 0,
        failedTrials: 0,
        skippedTrials: 0,
        totalTrials: 0,
      },
      costStatus: "loading",
      qa: { accepted: 2, rejected: 0, running: 0, failed: 1, unreviewed: 0 },
      reviewFilter: "rejected",
      onReviewFilter: () => {},
    })
  );
  assert.match(html, /Verdicts/);
  assert.match(html, /2 Accepted/);
  assert.match(html, /1 Failed/);
  assert.doesNotMatch(html, /0 (Rejected|Pending|No verdict)/);
  assert.match(html, /Show all tasks/);
});
