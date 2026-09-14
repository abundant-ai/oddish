import assert from "node:assert/strict";
import test from "node:test";
import {
  reasoningEffortOptions,
  REASONING_EFFORT_AGENTS,
} from "../src/lib/reasoning-effort.ts";
import { buildExperimentRunRequests } from "../src/lib/experiment-run.ts";

const standard = ["low", "medium", "high"];
const extended = [...standard, "xhigh", "max"];

for (const [agent, model, expected] of [
  ["codex", "openai/gpt-5.6", extended],
  ["codex", "openai/gpt-5.4-codex", extended],
  ["codex", "openai/o3", standard],
  ["claude-code", "global.anthropic.claude-opus-5", extended],
  ["gemini-cli", "google/gemini-3.1-pro-preview", ["low", "high"]],
  ["gemini-cli", "gemini/gemini-3.5-flash", ["minimal", ...standard]],
  ["antigravity-cli", "google/gemini-3.7-flash", ["minimal", ...standard]],
  ["antigravity-cli", "google/gemini-3.1-pro-preview", ["low", "high"]],
  ["cursor-cli", "anthropic/claude-opus-5[context=1m]", extended],
  [
    "grok-build",
    "xai/vendor-latest-learnability",
    ["none", "minimal", ...extended],
  ],
  ["mini-swe-agent", "openai/gpt-5.6", extended],
  ["mini-swe-agent", "anthropic/claude-sonnet-4-6", standard],
  ["mini-swe-agent", "gemini/gemini-3.1-pro-preview", ["low", "high"]],
  ["aider", "openai/gpt-5.6", extended],
  ["openhands", "openai/gpt-5.6", extended],
  ["copilot-cli", "gpt-5.6", [...standard, "xhigh"]],
  ["dsh", "gpt-5.6", ["low", "high", "max"]],
  ["tbh", "openai/gpt-5.6", ["none", "minimal", ...standard, "xhigh", "ultra"]],
] as [string, string, string[]][]) {
  test(`${agent} / ${model} exposes and submits its effort choices`, () => {
    assert.ok(REASONING_EFFORT_AGENTS.includes(agent));
    assert.deepEqual(reasoningEffortOptions(agent, model), expected);
    const [request] = buildExperimentRunRequests(
      ["task"],
      "experiment",
      agent,
      model,
      expected,
      1,
      "operation"
    );
    const body = JSON.parse(request.body) as {
      configs: {
        model: string;
        agent_config: { kwargs: { reasoning_effort: string } };
      }[];
    };
    assert.deepEqual(
      body.configs.map((config) => config.agent_config.kwargs.reasoning_effort),
      expected
    );
    assert.ok(body.configs.every((config) => config.model === model));
  });
}

test("Gemini 2.5, unknown models, and embedded Cursor effort keep agent default", () => {
  for (const agent of ["gemini-cli", "antigravity-cli", "mini-swe-agent"]) {
    assert.deepEqual(
      reasoningEffortOptions(agent, "google/gemini-2.5-flash"),
      []
    );
  }
  assert.deepEqual(
    reasoningEffortOptions(
      "cursor-cli",
      "openai/gpt-5.6[context=1m,effort=high]"
    ),
    []
  );
  assert.deepEqual(
    reasoningEffortOptions("unknown-agent", "openai/gpt-5.6"),
    []
  );
  assert.deepEqual(reasoningEffortOptions("codex", "openai/gpt-4o"), []);
  assert.deepEqual(reasoningEffortOptions("grok-build", ""), []);
});

test("agent and model matching ignores input case and whitespace", () => {
  assert.deepEqual(
    reasoningEffortOptions(" CODEX ", " OpenAI/GPT-5.6 "),
    extended
  );
});
