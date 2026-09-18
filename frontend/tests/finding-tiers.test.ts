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
// Every vote control the overview mounts, and every request a vote sends.
type Submit = (vote: "agree" | "disagree", note?: string) => Promise<void>;
const controls: { label: string; onSubmit: Submit }[] = [];
const requests: { url: string; body: Record<string, unknown> }[] = [];
// Render the real overview and findings; isolate routing, fetching, and controls.
const mocks: Record<string, unknown> = {
  "next/navigation": {
    useRouter: () => ({}),
    useSearchParams: () => new URLSearchParams(),
  },
  swr: () => ({ data: [] }),
  "@/lib/api": {
    fetcher: async (url: string, init: { body: string }) => {
      requests.push({ url, body: JSON.parse(init.body) });
    },
  },
  "@/components/ui/skeleton": { Skeleton: hidden },
  "@/components/analysis-prose": { AnalysisProse: hidden },
  "@/components/qa-report/copy-json-button": { CopyJsonButton: hidden },
  "@/components/qa-report/feedback-control": {
    FeedbackControl: (control: { label: string; onSubmit: Submit }) => {
      controls.push(control);
      return null;
    },
  },
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
    name.startsWith("@/components/") && !/\/(tokens|types)$/.test(name)
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

test("hosted overview votes on audit findings, trial findings, and analyses", async () => {
  controls.length = 0;
  requests.length = 0;
  const auditFinding: PreTrialFinding = { id: "audit-1", title: "Audit finding" };
  const trialFinding: PreTrialFinding = { id: "item-1", title: "Trial finding" };
  const trial = {
    id: "trial-1",
    agent: "codex",
    model: "openai/gpt-5",
    task_version: 2,
    status: "completed",
    created_at: "2026-09-17T00:00:00Z",
    analysis_status: "success",
    analysis: { classification: "BAD_FAILURE", action_items: [trialFinding] },
  } as Trial;
  const render = (apiBaseUrl: string) =>
    renderToStaticMarkup(
      React.createElement(TaskOverviewPanel, {
        taskId: "task-1",
        apiBaseUrl,
        version: 2,
        checksStatus: "success",
        checksFindings: [auditFinding],
        checksTrialId: "task-1-audit",
        scopeTrials: [trial],
        onRerunChecks: hidden,
        checksRerunning: false,
      })
    );

  render("/api");
  assert.deepEqual(
    controls.map((c) => c.label).sort(),
    [
      "action item: Audit finding",
      "action item: Trial finding",
      "the Bad failure analysis of codex · gpt-5",
    ]
  );
  for (const control of controls) await control.onSubmit("agree");
  await controls[0].onSubmit("disagree", " not in this task ");
  assert.ok(requests.every((r) => r.url === "/api/tasks/task-1/feedback"));
  assert.deepEqual(
    requests.map((r) => r.body),
    [
      {
        body: "",
        target: "qa_action_item",
        target_key: "audit-1",
        vote: "agree",
        trial_id: "task-1-audit",
      },
      {
        body: "",
        target: "qa_action_item",
        target_key: "item-1",
        vote: "agree",
        trial_id: "trial-1",
      },
      {
        body: "",
        target: "qa_verdict",
        target_key: "BAD_FAILURE",
        vote: "agree",
        trial_id: "trial-1",
      },
      {
        body: "not in this task",
        target: "qa_action_item",
        target_key: "audit-1",
        vote: "disagree",
        trial_id: "task-1-audit",
      },
    ]
  );

  controls.length = 0;
  render("/api/public/share-1");
  assert.equal(controls.length, 0);
});

test("an audit without a recorded trial id takes no finding vote", () => {
  controls.length = 0;
  renderToStaticMarkup(
    React.createElement(TaskOverviewPanel, {
      taskId: "task-1",
      apiBaseUrl: "/api",
      version: 2,
      checksStatus: "success",
      checksFindings: [{ id: "audit-1", title: "Audit finding" }],
      scopeTrials: [
        {
          id: "trial-1",
          agent: "codex",
          task_version: 2,
          status: "completed",
          created_at: "2026-09-17T00:00:00Z",
          analysis_status: "success",
          analysis: {
            classification: "GOOD_FAILURE",
            action_items: [{ id: "item-1", title: "Trial finding" }],
          },
        } as Trial,
      ],
      onRerunChecks: hidden,
      checksRerunning: false,
    })
  );
  assert.deepEqual(
    controls.map((c) => c.label).sort(),
    ["action item: Trial finding", "the Good failure analysis of codex"]
  );
});
