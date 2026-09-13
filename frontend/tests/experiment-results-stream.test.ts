import assert from "node:assert/strict";
import test from "node:test";
import { readExperimentResults } from "../src/lib/experiment-results-stream.ts";

function records(taskCount = 1, trialCount = 1) {
  return [
    {
      type: "experiment",
      experiment: {
        tasks: [],
        summary: { task_count: taskCount, trial_count: trialCount },
      },
    },
    { type: "task", task: { id: "task-1", name: "résumé 🧪" } },
    { type: "trial", trial: { id: "trial-1", task_id: "task-1" } },
    { type: "complete" },
  ];
}
const encode = (value: unknown[]) =>
  new TextEncoder().encode(
    value.map((v) => JSON.stringify(v)).join("\n") + "\n"
  );

test("reads individual records and UTF-8 text across arbitrary network boundaries", async () => {
  const bytes = encode(records());
  const response = new Response(
    new ReadableStream({
      start(controller) {
        for (let i = 0; i < bytes.length; i += 3)
          controller.enqueue(bytes.slice(i, i + 3));
        controller.close();
      },
    })
  );
  let updates = 0;
  const result = await readExperimentResults(response, () => updates++);
  assert.equal(result.experiment.tasks[0].name, "résumé 🧪");
  assert.equal(result.trials.length, 1);
  assert.equal(updates, 4);
});

test("exposes results before the response finishes", async () => {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const response = new Response(
    new ReadableStream({
      start(c) {
        controller = c;
      },
    })
  );
  let sawTask!: () => void;
  const taskVisible = new Promise<void>((resolve) => (sawTask = resolve));
  const result = readExperimentResults(response, (value) => {
    if (value.experiment.tasks.length) sawTask();
  });
  controller.enqueue(encode(records().slice(0, 2)));
  await taskVisible;
  controller.enqueue(encode(records().slice(2)));
  controller.close();
  assert.equal((await result).trials.length, 1);
});

for (const [name, values] of [
  ["truncated body", records().slice(0, 3)],
  ["missing results", records(2, 2)],
  ["missing header", records().slice(1)],
  [
    "extra data after completion",
    [...records(), { type: "task", task: { id: "extra" } }],
  ],
] as const) {
  test(`rejects ${name}`, async () => {
    await assert.rejects(
      readExperimentResults(new Response(encode([...values])), () => {})
    );
  });
}

test("propagates HTTP errors before parsing stream records", async () => {
  await assert.rejects(
    readExperimentResults(
      new Response(JSON.stringify({ detail: "Experiment not found" }), {
        status: 404,
      }),
      () => {}
    ),
    /Experiment not found/
  );
});
