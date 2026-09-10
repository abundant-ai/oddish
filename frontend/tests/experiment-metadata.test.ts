import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";

test("experiment metadata resolves without credentials or backend requests", async () => {
  const source = readFileSync(
    new URL(
      "../src/app/(app)/experiments/[experiment]/page.tsx",
      import.meta.url
    ),
    "utf8"
  );
  const exports =
    {} as typeof import("../src/app/(app)/experiments/[experiment]/page");
  runInNewContext(
    ts.transpileModule(source, {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        jsx: ts.JsxEmit.ReactJSX,
      },
    }).outputText,
    {
      exports,
      require(name: string) {
        if (name === "@/lib/utils")
          return { decodeExperimentRouteParam: decodeURIComponent };
        if (name === "./experiment-client" || name === "react/jsx-runtime")
          return {};
        throw new Error(`Metadata unexpectedly depends on ${name}`);
      },
      fetch() {
        throw new Error("Metadata must not fetch");
      },
    }
  );
  const metadata = await exports.generateMetadata({
    params: Promise.resolve({ experiment: "experiment-7" }),
  });
  assert.equal(metadata.title, "Experiment experiment-7 · Oddish");
  assert.equal(metadata.openGraph?.title, metadata.title);
  assert.equal(metadata.twitter?.title, metadata.title);
});
