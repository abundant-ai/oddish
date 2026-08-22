import assert from "node:assert/strict";
import test from "node:test";

import { fetcher } from "../src/lib/api.ts";

test("throws the API detail for a failed mutation", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ detail: "feedback rejected" }), {
      status: 422,
      statusText: "Unprocessable Entity",
      headers: { "Content-Type": "application/json" },
    });

  try {
    await assert.rejects(
      fetcher("/api/feedback", { method: "POST" }),
      /feedback rejected/
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("returns JSON after a successful mutation", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (_input, init) => {
    assert.equal(init?.method, "POST");
    assert.equal(init?.credentials, "include");
    return Response.json({ id: "feedback-1" });
  };

  try {
    assert.deepEqual(
      await fetcher<{ id: string }>("/api/feedback", { method: "POST" }),
      { id: "feedback-1" }
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});
