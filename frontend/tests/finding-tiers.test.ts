import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { runInNewContext } from "node:vm";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";
import type { PreTrialFinding, Task, Trial } from "../src/lib/types.ts";

const require = createRequire(import.meta.url);
const hidden = () => null;
// Render the real overview and findings; isolate routing, fetching, and controls.
const mocks: Record<string, unknown> = {
  "next/navigation": {
    useRouter: () => ({}),
    useSearchParams: () => new URLSearchParams(),
  },
  swr: () => ({ data: [] }),
  "@/lib/api": { fetcher: hidden },
  "@/components/ui/skeleton": { Skeleton: hidden },
  "@/components/analysis-prose": { AnalysisProse: hidden },
  "@/components/qa-report/copy-json-button": { CopyJsonButton: hidden },
  "@/components/qa-report/feedback-control": { FeedbackControl: hidden },
  "@/components/task-verdict-badge": {
    TaskVerdictBadge: () =>
      React.createElement("span", null, "Overall verdict"),
  },
};
const cache: Record<string, unknown> = {};
function load(name: string, parent = ""): unknown {
  if (name.startsWith(".")) {
    name = `${parent.slice(0, parent.lastIndexOf("/"))}/${name.slice(2)}`;
  }
  if (name in mocks) return mocks[name];
  if (!name.startsWith("@/")) return require(name);
  if (name in cache) return cache[name];
  const exports = {};
  cache[name] = exports;
  const extension =
    name.startsWith("@/components/") && !name.endsWith("/tokens")
      ? "tsx"
      : "ts";
  runInNewContext(
    ts.transpileModule(
      readFileSync(
        new URL(`../src/${name.slice(2)}.${extension}`, import.meta.url),
        "utf8"
      ),
      {
        compilerOptions: {
          module: ts.ModuleKind.CommonJS,
          jsx: ts.JsxEmit.ReactJSX,
        },
      }
    ).outputText,
    { exports, require: (child: string) => load(child, name), URLSearchParams }
  );
  return exports;
}
const { FindingList } = load(
  "@/components/qa-report/action-items"
) as typeof import("../src/components/qa-report/action-items.tsx");
const { TaskOverviewPanel } = load(
  "@/components/task-overview-panel"
) as typeof import("../src/components/task-overview-panel.tsx");

test("legacy findings render in priority order with their recorded badges", () => {
  const items: PreTrialFinding[] = [
    { id: "optional", title: "No priority" },
    { id: "converted", title: "Converted finding", severity: "must_fix" },
    {
      id: "required",
      title: "Legacy defect",
      tier: null,
      severity: "must_fix",
    },
  ];
  const html = renderToStaticMarkup(
    React.createElement(FindingList, { items })
  );
  const rows = [...html.matchAll(/<summary\b[^>]*>(.*?)<\/summary>/g)].map(
    (match) => match[1].replace(/<[^>]*>/g, "")
  );
  assert.equal(rows.length, 3);
  assert.match(rows[0], /Must fixConverted finding$/);
  assert.match(rows[1], /Must fixLegacy defect$/);
  assert.doesNotMatch(html, /SHOULD FIX|should_fix/);
  assert.match(rows[2], /RECORDED OPTIONALNo priority$/);
});

for (const [description, fields, required] of [
  ["absent tier", { severity: "must_fix" }, true],
  ["null tier", { tier: null, severity: "must_fix" }, true],
  ["explicit optional tier", { tier: "optional", severity: "must_fix" }, false],
  ["explicit must-fix tier", { tier: "must_fix", severity: "optional" }, true],
  ["legacy optional severity", { severity: "optional" }, false],
  ["missing priority", { tier: null, severity: null }, false],
] as const) {
  for (const source of ["audit", "run"] as const) {
    test(`${source} finding with ${description} has matching badge, count, and verdict visibility`, () => {
      const finding: PreTrialFinding = {
        id: "defect",
        title: "Stored finding",
        ...fields,
      };
      const html = renderToStaticMarkup(
        React.createElement(TaskOverviewPanel, {
          taskId: "task-1",
          version: 1,
          checksStatus: "success",
          checksFindings: source === "audit" ? [finding] : [],
          scopeTrials:
            source === "run"
              ? [
                  {
                    id: "trial-1",
                    agent: "codex",
                    task_version: 1,
                    status: "completed",
                    created_at: "2026-09-14T00:00:00Z",
                    analysis_status: "success",
                    analysis: {
                      classification: "GOOD_FAILURE",
                      action_items: [finding],
                    },
                  } as Trial,
                ]
              : [],
          verdictTask: { id: "task-1" } as Task,
          onRerunChecks: hidden,
          checksRerunning: false,
        })
      );
      const text = html.replace(/<[^>]*>/g, "");
      assert.ok(text.includes(required ? "1 Must fix" : "1 finding"), text);
      assert.ok(
        text.includes(
          required
            ? "Must fixStored finding"
            : "RECORDED OPTIONALStored finding"
        ),
        text
      );
      assert.equal(text.includes("Overall verdict"), !required, text);
    });
  }
}
