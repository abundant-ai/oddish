import assert from "node:assert/strict";
import test from "node:test";
import {
  buildExperimentRunRequests,
  submitExperimentRuns,
} from "../src/lib/experiment-run.ts";
import { reasoningEffortOptions } from "../src/lib/reasoning-effort.ts";

test("run plan preserves model ID and sends distinct per-effort configs", () => {
  const requests = buildExperimentRunRequests(
    ["a", "b"],
    "exp",
    "claude-code",
    "global.anthropic.claude-opus-5",
    ["low", "high"],
    5,
    "operation"
  );
  assert.equal(requests.length, 2);
  const body = JSON.parse(requests[0].body);
  assert.equal(body.add_trials, true);
  assert.equal(body.experiment_id, "exp");
  assert.deepEqual(
    body.configs.map(
      (c: {
        model: string;
        n_trials: number;
        agent_config: { kwargs: { reasoning_effort: string } };
      }) => [c.model, c.n_trials, c.agent_config.kwargs.reasoning_effort]
    ),
    [
      ["global.anthropic.claude-opus-5", 5, "low"],
      ["global.anthropic.claude-opus-5", 5, "high"],
    ]
  );
  assert.notEqual(requests[0].key, requests[1].key);
});
test("agent default omits kwargs and invalid counts cannot launch", () => {
  const [request] = buildExperimentRunRequests(
    ["a"],
    "exp",
    "codex",
    "openai/gpt-5.6",
    ["default"],
    1,
    "op"
  );
  assert.equal(JSON.parse(request.body).configs[0].agent_config, undefined);
  for (const n of [0, 1.5, 101, NaN])
    assert.throws(() =>
      buildExperimentRunRequests(["a"], "e", "codex", "m", ["high"], n, "op")
    );
  assert.deepEqual(reasoningEffortOptions("gemini-cli", "google/gemini"), []);
});
test("partial failure retries only failed requests with original bodies and keys", async () => {
  const requests = buildExperimentRunRequests(
    ["a", "b", "c", "d", "e"],
    "exp",
    "codex",
    "openai/gpt-5.6",
    ["high"],
    5,
    "op"
  );
  let active = 0,
    peak = 0;
  const first = await submitExperimentRuns(
    requests,
    async (_url, init) => {
      active++;
      peak = Math.max(peak, active);
      await new Promise((resolve) => setTimeout(resolve, 1));
      active--;
      return new Response(JSON.stringify({ detail: "unavailable" }), {
        status: JSON.parse(init.body as string).task_id === "b" ? 503 : 200,
      });
    },
    () => {}
  );
  assert.equal(peak, 4);
  assert.deepEqual(first.failed, [requests[1]]);
  const sent: RequestInit[] = [];
  const retry = await submitExperimentRuns(
    first.failed,
    async (_url, init) => {
      sent.push(init);
      return new Response("{}");
    },
    () => {}
  );
  assert.equal(retry.failed.length, 0);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].body, requests[1].body);
  assert.equal(
    (sent[0].headers as Record<string, string>)["Idempotency-Key"],
    requests[1].key
  );
});
