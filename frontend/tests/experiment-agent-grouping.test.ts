import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import ts from "typescript";

// Match the app's @/ alias while exercising the actual grouping and type helpers.
function load(file: string): Record<string, unknown> {
  const exports = {};
  const source = readFileSync(
    new URL(`../src/lib/${file}.ts`, import.meta.url),
    "utf8"
  );
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS },
  });
  runInNewContext(compiled.outputText, {
    exports,
    require: (name: string) => {
      assert.equal(name, "@/lib/types");
      return load("types");
    },
  });
  return exports;
}
const {
  buildExperimentAgentSummaries,
  getExperimentAgentKey,
  compareReasoningEffort,
  experimentModelLabel,
} = load(
  "experiment-agent-grouping"
) as typeof import("../src/lib/experiment-agent-grouping.ts");
import type { Task, Trial } from "../src/lib/types.ts";

const model = "global.anthropic.claude-opus-5";
const trial = (effort: string | null): Trial =>
  ({
    agent: "claude-code",
    model,
    kind: "agent",
    reasoning_effort: effort,
  }) as Trial;

test("four efforts produce four columns, each with five repetitions", () => {
  const trials = ["low", "medium", "high", "xhigh"].flatMap((effort) =>
    Array.from({ length: 5 }, () => trial(effort))
  );
  const summaries = buildExperimentAgentSummaries([{ trials } as Task]);
  assert.equal(summaries.length, 4);
  for (const summary of summaries)
    assert.equal(
      trials.filter((t) => getExperimentAgentKey(t) === summary.key).length,
      5
    );
  assert.deepEqual(
    [...summaries].map((s) => s.reasoningEffort),
    ["low", "medium", "high", "xhigh"]
  );
});
test("unspecified effort stays separate and keys remain stable as more trials arrive", () => {
  const first = trial(null);
  const before = buildExperimentAgentSummaries([{ trials: [first] } as Task])[0]
    .key;
  const after = buildExperimentAgentSummaries([
    { trials: [first, trial("low"), trial("high")] } as Task,
  ]);
  assert.equal(before, after[0].key);
  assert.equal(after.length, 3);
  assert.equal(experimentModelLabel(model, null), model);
  assert.equal(experimentModelLabel(model), model);
  assert.equal(experimentModelLabel(null, null), "default");
  assert.equal(experimentModelLabel(model, "none"), `${model}/none`);
  assert.equal(experimentModelLabel(model, "high"), `${model}/high`);
  assert.equal(after[0].label, `claude-code/${model}`);
  assert.equal(after[1].label, `claude-code/${model}/low`);
  assert.equal(first.model, model);
});
test("baselines, QA and probes retain their own groups", () => {
  assert.equal(getExperimentAgentKey({ ...trial(null), agent: "nop" }), "nop");
  assert.equal(
    getExperimentAgentKey({ ...trial(null), agent: "oracle" }),
    "oracle"
  );
  assert.equal(
    getExperimentAgentKey({ ...trial("high"), is_probe: true }),
    "probe"
  );
  assert.equal(getExperimentAgentKey({ ...trial("high"), kind: "qa" }), "qa");
});
test("efforts sort in execution order, not alphabetically", () => {
  assert.deepEqual(
    ["xhigh", "high", "low", "medium"].sort(compareReasoningEffort),
    ["low", "medium", "high", "xhigh"]
  );
});

test("new runner efforts sort with the existing effort levels", () => {
  assert.deepEqual(
    [
      "ultracode",
      "ultra",
      "max",
      "xhigh",
      "high",
      "medium",
      "low",
      "minimal",
      "none",
    ].sort(compareReasoningEffort),
    [
      "none",
      "minimal",
      "low",
      "medium",
      "high",
      "xhigh",
      "max",
      "ultra",
      "ultracode",
    ]
  );
});

test("grouping efforts combines five trials and keeps their original settings", () => {
  const trials = [null, null, "high", "high", "high"].map(trial);
  const task = { trials } as Task;
  const summaries = buildExperimentAgentSummaries([task], true);
  assert.equal(summaries.length, 1);
  assert.equal(summaries[0].label, `claude-code/${model}`);
  assert.equal(summaries[0].reasoningEffort, null);
  assert.equal(
    trials.filter((t) => getExperimentAgentKey(t, true) === summaries[0].key)
      .length,
    5
  );
  assert.deepEqual(
    trials.map((t) => t.reasoning_effort),
    [null, null, "high", "high", "high"]
  );
  assert.equal(buildExperimentAgentSummaries([task], false).length, 2);
});

test("grouping efforts keeps different agents, models, and internal groups separate", () => {
  const trials = [
    trial("low"),
    trial("high"),
    { ...trial("high"), agent: "mini-swe-agent" },
    { ...trial("high"), model: "another-model" },
    { ...trial("high"), agent: "nop" },
    { ...trial("high"), agent: "oracle" },
    { ...trial("high"), kind: "qa" as const },
    { ...trial("high"), is_probe: true },
  ];
  const summaries = buildExperimentAgentSummaries([{ trials } as Task], true);
  assert.equal(summaries.length, 7);
  assert.equal(getExperimentAgentKey(trials[4], true), "nop");
  assert.equal(getExperimentAgentKey(trials[5], true), "oracle");
  assert.equal(getExperimentAgentKey(trials[6], true), "qa");
  assert.equal(getExperimentAgentKey(trials[7], true), "probe");
});
