import assert from "node:assert/strict";
import test from "node:test";
import {
  endpointKey,
  modelCatalogRows,
  type ModelCheckState,
  type ModelSort,
} from "../src/lib/model-catalog.ts";
import type { ModelEndpointSummary } from "../src/lib/types.ts";

const models: ModelEndpointSummary[] = [
  {
    model: "anthropic/claude-3-7-sonnet-20250219",
    provider: "anthropic",
    route: "anthropic",
    credential: null,
    testable: true,
  },
  {
    model: "anthropic/claude-3.7-sonnet",
    provider: "anthropic",
    route: "anthropic",
    credential: null,
    testable: true,
  },
  {
    model: "openai/gpt-10",
    provider: "openai",
    route: "azure",
    credential: null,
    testable: true,
  },
  {
    model: "openai/gpt-2",
    provider: "openai",
    route: "openai",
    credential: null,
    testable: true,
  },
  {
    model: "cursor/auto",
    provider: "cursor",
    route: "cursor",
    credential: null,
    testable: false,
  },
];
const byName: ModelSort = { field: "name", direction: "asc" };
const checks: Record<string, ModelCheckState> = {
  [endpointKey(models[0])]: {
    status: "complete",
    testedAt: 100,
    result: {
      ...models[0],
      resolved_model: models[0].model,
      transport: "litellm_completion",
      ok: true,
      latency_ms: 200,
      response: "Hello",
      error: null,
      failure_kind: null,
      status_code: null,
      request_id: null,
    },
  },
  [endpointKey(models[1])]: {
    status: "complete",
    testedAt: 200,
    result: {
      ...models[1],
      resolved_model: models[1].model,
      transport: "litellm_completion",
      ok: false,
      latency_ms: 50,
      response: "",
      error: "Provider returned no text response.",
      failure_kind: "provider",
      status_code: null,
      request_id: null,
    },
  },
  [endpointKey(models[2])]: {
    status: "error",
    testedAt: 300,
    message: "Request failed",
  },
};

test("search ignores punctuation, case and word order while retaining distinct versions", () => {
  for (const query of [
    "SONNET claude 3.7",
    "claude-3-7 sonnet",
    "sonnet anthropic",
  ]) {
    const rows = modelCatalogRows(models, {}, query, "all", "all", byName);
    assert.deepEqual(
      new Set(rows.map(({ endpoint }) => endpoint.model)),
      new Set(models.slice(0, 2).map(({ model }) => model))
    );
  }
  assert.equal(
    modelCatalogRows(models, {}, "claude gpt", "all", "all", byName).length,
    0
  );
  assert.equal(
    modelCatalogRows(models, {}, "Azure OpenAI", "all", "all", byName)[0]
      .endpoint,
    models[2]
  );
});

test("combines search, exact provider route, and status without including CLI-only models as untested", () => {
  assert.equal(
    modelCatalogRows(models, checks, "gpt", "azure", "Failed", byName)[0]
      .endpoint,
    models[2]
  );
  assert.equal(
    modelCatalogRows(models, checks, "gpt", "openai", "Failed", byName).length,
    0
  );
  assert.deepEqual(
    modelCatalogRows(models, checks, "", "all", "Not tested", byName).map(
      ({ endpoint }) => endpoint
    ),
    [models[3]]
  );
  assert.equal(
    modelCatalogRows(models, checks, "", "all", "CLI only", byName)[0].endpoint,
    models[4]
  );
  assert.equal(
    modelCatalogRows(
      models,
      { [endpointKey(models[0])]: { status: "running" } },
      "",
      "all",
      "Testing",
      byName
    )[0].endpoint,
    models[0]
  );
  assert.equal(
    modelCatalogRows(models, checks, "", "all", "Passed", byName)[0].endpoint,
    models[0]
  );
  assert.equal(
    modelCatalogRows(models, checks, "", "all", "Failed", byName).length,
    2
  );
});

test("sorts measurements in both directions with missing values last, including zero latency", () => {
  for (const field of ["latency", "testedAt"] as const) {
    for (const direction of ["asc", "desc"] as const) {
      const rows = modelCatalogRows(models, checks, "", "all", "all", {
        field,
        direction,
      });
      const values = rows.map((row) => row[field]);
      const measured = values.filter((value) => value !== null);
      assert.deepEqual(
        values.slice(0, measured.length),
        [...measured].sort((a, b) => (direction === "asc" ? a - b : b - a))
      );
      assert.ok(values.slice(measured.length).every((value) => value === null));
    }
  }
  const passed = checks[endpointKey(models[0])];
  assert.equal(passed.status, "complete");
  if (passed.status !== "complete") throw new Error("Expected completed check");
  const zero = {
    ...checks,
    [endpointKey(models[0])]: {
      ...passed,
      result: { ...passed.result, latency_ms: 0 },
    },
  };
  assert.equal(
    modelCatalogRows(models, zero, "", "all", "all", {
      field: "latency",
      direction: "asc",
    })[0].latency,
    0
  );
});

test("sorts model numbers naturally without mutating the catalog or losing route identity", () => {
  const original = [...models];
  const sameModelOtherRoute = { ...models[3], route: "azure" };
  const rows = modelCatalogRows(
    [...models, sameModelOtherRoute],
    {},
    "gpt",
    "all",
    "all",
    byName
  );
  assert.deepEqual(
    rows.map(({ endpoint }) => endpoint.model),
    ["openai/gpt-2", "openai/gpt-2", "openai/gpt-10"]
  );
  assert.equal(new Set(rows.map(({ key }) => key)).size, 3);
  assert.deepEqual(models, original);
});
