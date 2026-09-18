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
  Component?: React.ComponentType<{
    task: Task;
    variant: "card" | "inline" | "summary";
    mustFixCount?: number;
    rejectionSource?: "Pre-trial audit" | "Trial analysis";
  }>;
  present?: (
    task: Task,
    iconSize: string,
    active: boolean,
    count?: number
  ) => { title: string; isGood: boolean | null };
} = {};
runInNewContext(
  ts.transpileModule(
    `${presentation.getText(badgeSource)}\n${badgeSource.statements.find((node) => ts.isFunctionDeclaration(node) && node.name?.text === "TaskVerdictBadge")!.getText(badgeSource)}\nexports.present = presentVerdict; exports.Component = TaskVerdictBadge;`,
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
    Card: box,
    CardHeader: box,
    CardTitle: box,
    CardContent: box,
    AnalysisProse: ({ text }: { text: string }) =>
      React.createElement("p", null, text),
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
      assert.ok(!html.includes(review.VERDICT_LABELS.outdated), html);
      assert.equal(html.includes("2 completed runs"), ungradedSettled === 2);
    }
    const outdated = renderToStaticMarkup(
      React.createElement(exports.Chip, {
        task: { ...reviewed, review_version_matches: false },
        ungradedSettled: 0,
      })
    );
    assert.ok(outdated.includes(review.VERDICT_LABELS.outdated), outdated);
  });
}

test("missing and inconclusive verdicts remain unreviewed", () => {
  assert.equal(review.taskReviewStatus(task), "missing");
  assert.equal(review.taskReviewFilter(task), "unreviewed");
  assert.equal(
    review.taskReviewStatus({
      ...task,
      verdict: { is_good: null, confidence: null },
    }),
    "missing"
  );
  assert.equal(
    review.taskReviewStatus({ ...task, verdict_status: null }),
    "never"
  );
  assert.equal(
    review.taskReviewFilter({ ...task, verdict_status: null }),
    "unreviewed"
  );
});

test("QA verdict failure retains its cause and remains distinct from rejection", () => {
  const reason = "Evidence could not be read.";
  const presented = badge.present!(
    { ...task, verdict_status: "failed", verdict_error: reason },
    "",
    false
  );
  assert.equal(presented.title, "Verdict failed");
  assert.equal(presented.detail, reason);
  assert.equal(presented.isGood, null);
  assert.equal(review.REVIEW_LABELS.error, "Verdict failed");
  assert.equal(review.REVIEW_LABELS.accepted, "Verdict accepted");
});

test("a settled task without eligible runs is pending solver runs, not failed", () => {
  const reason =
    "Insufficient evidence: no eligible solver trials for the current task version.";
  const noEvidence = { ...task, verdict_status: "failed", verdict_error: reason };
  assert.equal(review.taskReviewStatus(noEvidence), "no_evidence");
  assert.equal(review.taskReviewFilter(noEvidence), "unreviewed");
  const presented = badge.present!(noEvidence, "", false);
  assert.equal(presented.title, "Verdict pending: needs solver runs");
  assert.equal(presented.detail, reason);
  assert.equal(presented.isGood, null);
  const html = renderToStaticMarkup(
    React.createElement(exports.Chip, { task: noEvidence, ungradedSettled: 0 })
  );
  assert.ok(html.includes("Verdict pending: needs solver runs"), html);
  assert.ok(html.includes(reason), html);
  assert.ok(!html.includes("bg-amber-100"), html);
});

test("analysis progress separates completed classifications from failed and pending analysis", () => {
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
    review.runReviewSummary(
      reviewed.trials.filter(
        (trial) => trial.task_version_id === reviewed.trial_version_id
      )
    ),
    "5/9 analyzed · 1 analysis failed · 1 analyzing · 1 queued"
  );
});

test("accepted and missing verdict chips have concise exact labels", () => {
  for (const [verdict, label] of [
    [{ verdict: "accept", is_good: true, confidence: null }, "Verdict accepted"],
    [null, "Verdict pending: none recorded"],
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
  assert.match(html, /Verdict rejected/);
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
  assert.equal((html.match(/Verdict rejected: 1 Must fix/g) ?? []).length, 1);
  assert.doesNotMatch(html, /Must fix Finding/);
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
  assert.match(html, /QA verdicts/);
  assert.match(html, /2 Accepted/);
  assert.match(html, /1 Failed/);
  assert.doesNotMatch(html, /0 (Rejected|Pending)/);
  assert.match(html, /Show all tasks/);
});

test("completed analysis counts runs with infrastructure errors as analyzed", () => {
  const trials = Array.from(
    { length: 15 },
    (_, index) =>
      ({
        agent: "codex",
        analysis_status: "success",
        analysis: {
          classification:
            index < 8
              ? "HARNESS_ERROR"
              : index < 13
                ? "GOOD_FAILURE"
                : "GOOD_SUCCESS",
        },
      }) as Trial
  );
  assert.equal(review.runReviewCounts(trials).analyzed, 15);
  assert.equal(review.runReviewCounts(trials).failed, 0);
  assert.equal(review.runReviewSummary(trials), "15/15 analyzed");
  assert.equal(review.EXECUTION_LABELS.GOOD_FAILURE, "Good failure");
  assert.equal(review.runReviewSummary([]), "No runs");
});

test("routine fetch narration is absent while errors retain a retry action", () => {
  const module: {
    ExperimentResultsStatus?: React.ComponentType<Record<string, unknown>>;
  } = {};
  runInNewContext(
    ts.transpileModule(
      readFileSync(
        new URL(
          "../src/components/experiment-results-status.tsx",
          import.meta.url
        ),
        "utf8"
      ),
      {
        compilerOptions: {
          module: ts.ModuleKind.CommonJS,
          jsx: ts.JsxEmit.ReactJSX,
        },
      }
    ).outputText,
    {
      exports: module,
      require: (name: string) =>
        name === "react/jsx-runtime"
          ? jsx
          : {
              Alert: box,
              AlertDescription: box,
              AlertTitle: box,
              Button: box,
            },
    }
  );
  const render = (props: Record<string, unknown>) =>
    renderToStaticMarkup(
      React.createElement(module.ExperimentResultsStatus!, {
        tasksLoaded: 25,
        trialsLoaded: 1400,
        onRetry: () => {},
        ...props,
      })
    );
  for (const complete of [false, true])
    for (const isLoading of [false, true]) {
      assert.equal(render({ complete, isLoading, hasError: false }), "");
    }
  const failure = render({ complete: true, isLoading: false, hasError: true });
  assert.match(failure, /Could not refresh results/);
  assert.match(failure, /Retry/);
});

for (const verdictStatus of ["success", "running", "failed", null] as const) {
  test(`known required fixes stay visible with ${verdictStatus} review`, () => {
    const html = renderToStaticMarkup(
      React.createElement(exports.Chip, {
        task: {
          ...task,
          must_fix_count: 3,
          verdict_status: verdictStatus,
          verdict: null,
        },
        ungradedSettled: 0,
        onOpen: () => {},
      })
    );
    assert.equal(html.replace(/<[^>]*>/g, ""), "Verdict rejected: 3 Must fix");
    assert.match(html, /aria-label="Open findings for Task"/);
  });
}
for (const variant of ["inline", "summary", "card"] as const) {
  test(`${variant} verdict keeps the count and shows detail outside the summary`, () => {
    const html = renderToStaticMarkup(
      React.createElement(badge.Component!, {
        task: {
          ...task,
          must_fix_count: 1,
          verdict: {
            verdict: "reject",
            is_good: false,
            confidence: "high",
            primary_issue: "Duplicate explanation",
            recommendations: ["Duplicate fix"],
          },
        },
        variant,
        mustFixCount: 3,
      })
    );
    assert.equal((html.match(/3 Must fix/g) ?? []).length, 1);
    assert.doesNotMatch(html, /confidence|1 Must fix/);
    if (variant === "summary") {
      assert.equal(
        html.replace(/<[^>]*>/g, ""),
        "Verdict rejected: 3 Must fix"
      );
    } else {
      assert.match(html, /Duplicate explanation/);
      assert.match(html, /Duplicate fix/);
      assert.doesNotMatch(html, /<details open/);
    }
  });
}
test("task-page rejection identifies the audit and count without generated prose", () => {
  const html = renderToStaticMarkup(
    React.createElement(badge.Component!, {
      task: {
        ...task,
        must_fix_count: 2,
        verdict: {
          is_good: false,
          confidence: "high",
          primary_issue:
            "The source audit found two defects with a long explanation.",
        },
      },
      variant: "summary",
      rejectionSource: "Pre-trial audit",
    })
  );
  assert.equal(
    html.replace(/<[^>]*>/g, ""),
    "Verdict rejected · Pre-trial audit2 Must fix"
  );
});
test("selected version without findings does not reuse another version's count", () => {
  const presented = badge.present!(
    {
      ...task,
      must_fix_count: 3,
      review_version_matches: false,
      verdict: { verdict: "reject", is_good: false, confidence: null },
    },
    "",
    false,
    0
  );
  assert.equal(presented.title, review.VERDICT_LABELS.outdated);
});
