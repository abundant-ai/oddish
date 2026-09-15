import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";
import { encodeFilePath } from "../src/lib/file-path.ts";

for (const [path, encoded] of [
  ["tests/nested/test.sh", "tests/nested/test.sh"],
  ["artifacts/chart #1?.png", "artifacts/chart%20%231%3F.png"],
  ["notes/résumé 100%.txt", "notes/r%C3%A9sum%C3%A9%20100%25.txt"],
  ["literal%2Fname/file.txt", "literal%252Fname/file.txt"],
]) {
  test(`file URL preserves directory boundaries and filename: ${path}`, () => {
    assert.equal(encodeFilePath(path), encoded);
    const url = new URL(
      `https://example.test/files/${encoded}?max_bytes=102400`
    );
    assert.deepEqual(
      url.pathname.slice(7).split("/").map(decodeURIComponent),
      path.split("/")
    );
    assert.equal(url.searchParams.get("max_bytes"), "102400");
    assert.equal(url.hash, "");
  });
}

const proxies = [
  "tasks/[task_id]/files/[...path]",
  "trials/[trial_id]/files/[...path]",
  "public/experiments/[token]/tasks/[task_id]/files/[...path]",
  "public/experiments/[token]/trials/[trial_id]/files/[...path]",
];
for (const proxy of proxies) {
  const taskFile = proxy.includes("tasks/");
  for (const signed of taskFile ? [false, true] : [false]) {
    test(`${proxy} preserves paths and ${signed ? "renews signed URLs" : "caches text"}`, async () => {
      const responseData = signed
        ? {
            url: "https://storage.test/file?signature=1",
            expires_at: 1900000000,
          }
        : { content: "file contents" };
      const requests: URL[] = [];
      const exports: {
        GET?: (request: unknown, context: unknown) => Promise<Response>;
      } = {};
      const modules: Record<string, unknown> = {
        "@/lib/file-path": { encodeFilePath },
        "next/server": {
          NextResponse: {
            json: (data: unknown, init?: ResponseInit) =>
              Response.json(data, init),
          },
        },
        "@clerk/nextjs/server": {
          auth: async () => ({ getToken: async () => "fixture-token" }),
        },
        "@/lib/backend-config": {
          getBackendUrl: (endpoint: string, path: string) =>
            `https://backend.test/${endpoint}${path}`,
          getClerkToken: async () => "fixture-token",
          getAuthHeaders: () => ({}),
        },
        "@/lib/proxy-headers": {
          backendFetchHeaders: () => ({}),
          attachUpstreamServerTiming: (response: Response) => response,
        },
      };
      runInNewContext(
        ts.transpileModule(
          readFileSync(
            new URL(`../src/app/api/${proxy}/route.ts`, import.meta.url),
            "utf8"
          ),
          {
            compilerOptions: { module: ts.ModuleKind.CommonJS },
          }
        ).outputText,
        {
          exports,
          URL,
          Response,
          require: (name: string) => {
            assert.ok(name in modules, name);
            return modules[name];
          },
          fetch: async (input: string, init: RequestInit) => {
            requests.push(new URL(input));
            if (taskFile) assert.equal(init.cache, "no-store");
            return Response.json(responseData);
          },
        }
      );
      const path = ["nested folder", "résumé?#%2F.txt"];
      const query = `?max_bytes=102400&attempt=2&revision=rev-2${signed ? "&presign=1" : ""}`;
      const request = {
        url: `https://frontend.test/api/files/${encodeFilePath(path.join("/"))}${query}`,
        nextUrl: new URL(`https://frontend.test/${query}`),
      };
      const response = await exports.GET!(request, {
        params: Promise.resolve({
          task_id: "task",
          trial_id: "trial",
          token: "share-token",
          path,
        }),
      });
      assert.equal(response.status, 200);
      assert.equal(requests.length, 1);
      const [url] = requests;
      assert.deepEqual(
        url.pathname.split("/files/")[1].split("/").map(decodeURIComponent),
        path
      );
      assert.equal(url.search, query);
      assert.equal(url.hash, "");
      if (taskFile) {
        const scope = proxy.startsWith("public/") ? "public" : "private";
        assert.deepEqual(await response.json(), responseData);
        assert.equal(
          response.headers.get("cache-control"),
          signed
            ? scope === "private"
              ? "private, no-store"
              : "no-store"
            : `${scope}, max-age=300, stale-while-revalidate=60`
        );
      }
    });
  }
}
