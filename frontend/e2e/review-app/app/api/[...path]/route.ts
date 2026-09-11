import { NextRequest, NextResponse } from "next/server";
import { board, tasks, openFor, versionFor } from "../../../records";
export async function GET(request: NextRequest) {
  const parts = request.nextUrl.pathname
    .slice(5)
    .split("/")
    .map(decodeURIComponent);
  if (parts[0] === "deliveries") return NextResponse.json(board);
  const task = tasks.find((item) => item.id === parts[1]);
  if (parts[0] === "tasks" && task) {
    const versionId = request.nextUrl.searchParams.get("version_id");
    const version = Number(
      request.nextUrl.searchParams.get("version") ??
        versionId?.split("-v").pop() ??
        task.current_version
    );
    if (version === 404)
      return NextResponse.json(
        { detail: "Historical version unavailable" },
        { status: 404 }
      );
    const action = parts[2];
    if (action === "open") return NextResponse.json(openFor(task, version));
    if (action === "panel")
      return NextResponse.json({
        task: openFor(task, version).task,
        version: versionFor(task, version),
        can_retry: false,
        cancel: null,
        active_trials: 0,
        qa_active: false,
        can_run_qa: !["queued", "running"].includes(task.verdict_status ?? ""),
        has_analysis: true,
      });
    if (action === "trials") return NextResponse.json(task.trials);
    if (action === "versions")
      return NextResponse.json([
        versionFor(task, task.current_version!),
        versionFor(task, task.current_version! - 1),
      ]);
    if (action === "qa-history")
      return NextResponse.json({
        task_id: task.id,
        versions: [],
        unversioned_runs: [],
        verdict: task.verdict,
      });
    if (action === "files") {
      if (parts.length > 3) {
        if (parts.slice(3).join("/") !== "tests/test.sh")
          return NextResponse.json(
            { detail: `Historical file unavailable on v${version}` },
            { status: 404 }
          );
        return NextResponse.json({
          path: "tests/test.sh",
          content:
            "#!/bin/sh\n# verifier fixture\nanswer=$(cat /tmp/answer)\n# no empty-answer check\n# award credit\necho 1 > /tmp/reward\nexit 0\n",
          source_hash: `fixture-v${version}`,
        });
      }
      const directoryPage = (prefix: string) => ({
        files:
          prefix === "tests"
            ? [{ path: "tests/test.sh", key: "tests/test.sh", size: 140 }]
            : [],
        dirs: prefix === "" ? [{ path: "tests" }] : [],
        source_hash: `fixture-v${version}`,
        cursor: null,
      });
      const directories = request.nextUrl.searchParams.getAll("directories");
      return NextResponse.json(
        directories.length
          ? {
              directories: Object.fromEntries(
                directories.map((prefix) => [prefix, directoryPage(prefix)])
              ),
              version,
              source_hash: `fixture-v${version}`,
            }
          : directoryPage(request.nextUrl.searchParams.get("prefix") ?? "")
      );
    }
  }
  if (parts[0] === "trials") {
    const trial = tasks
      .flatMap((task) => task.trials ?? [])
      .find((item) => item.id === parts[1]);
    if (trial) return NextResponse.json(trial);
  }
  if (parts[0] === "tags") return NextResponse.json([]);
  if (parts[0] === "settings") return NextResponse.json({});
  return NextResponse.json(
    { detail: "Fixture resource unavailable" },
    { status: 404 }
  );
}
export async function POST(request: NextRequest) {
  // This fixture has no backend, credentials, queue, or paid operations.
  if (/\/qa\/(retry|pre-trial)$/.test(request.nextUrl.pathname))
    return NextResponse.json({ status: "queued" });
  return NextResponse.json(
    { detail: "Mutation outside fixture scope" },
    { status: 405 }
  );
}
