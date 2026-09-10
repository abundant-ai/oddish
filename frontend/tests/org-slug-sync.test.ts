import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";
import * as orgPath from "../src/lib/org-path.ts";

const componentCode = ts.transpileModule(
  readFileSync(
    new URL("../src/components/org-slug-sync.tsx", import.meta.url),
    "utf8"
  ),
  { compilerOptions: { module: ts.ModuleKind.CommonJS } }
).outputText;

function setup(pathname: string) {
  const auth = {
    isLoaded: true,
    organization: { id: "org_acme", slug: "acme" } as {
      id: string;
      slug: string;
    } | null,
  };
  const destinations: string[] = [];
  const effects: (() => void)[] = [];
  const exports: { OrgSlugSync?: () => null } = {};
  const modules: Record<string, unknown> = {
    "@clerk/nextjs": { useOrganization: () => auth },
    "next/navigation": { usePathname: () => pathname },
    react: { useEffect: (effect: () => void) => effects.push(effect) },
    "@/lib/org-path": orgPath,
  };
  // Exercise the component's redirect Effect with Clerk updated while the
  // browser still has the old URL, before either navigation can finish.
  runInNewContext(componentCode, {
    exports,
    require: (name: string) => {
      assert.ok(name in modules, `Unexpected import: ${name}`);
      return modules[name];
    },
    window: {
      location: {
        pathname,
        search: "?trial=old-org-trial",
        hash: "#old-org-output",
        assign: (destination: string) => destinations.push(destination),
      },
    },
  });
  return {
    auth,
    destinations,
    render() {
      exports.OrgSlugSync!();
      for (const effect of effects.splice(0)) effect();
    },
  };
}

test("an org change sends stale resource URLs to the new dashboard without query or fragment", () => {
  for (const resource of [
    "deliveries/delivery-1",
    "tasks/task-1",
    "tasks/task-1/probe/trial-1",
    "experiments/experiment-1",
  ]) {
    const page = setup(`/orgs/acme/${resource}`);
    page.render();
    assert.deepEqual(page.destinations, []);

    page.auth.organization = { id: "org_beta", slug: "beta" };
    page.render();
    assert.deepEqual(page.destinations, ["/orgs/beta/dashboard"]);
  }
});

test("loading Clerk or having no active organization does not redirect", () => {
  const page = setup("/orgs/acme/tasks/task-1");
  page.auth.organization = { id: "org_beta", slug: "beta" };
  page.auth.isLoaded = false;
  page.render();
  page.auth.isLoaded = true;
  page.auth.organization = null;
  page.render();
  assert.deepEqual(page.destinations, []);
});

test("matching org URLs and unprefixed URLs do not redirect", () => {
  for (const pathname of ["/orgs/acme/tasks/task-1", "/tasks/task-1"]) {
    const page = setup(pathname);
    page.render();
    assert.deepEqual(page.destinations, []);
  }
});
