import { requirementExamples } from "../../../requirement-examples";
import {
  pageFixture,
  selectionFixture,
} from "../../../../delivery-page-fixtures";
import { NextRequest, NextResponse } from "next/server";
import { board, tasks, openFor, versionFor } from "../../../records";
import {
  board as deliveryBoard,
  reviewTaskRow,
} from "../../../../delivery-fixtures";

const duplicationBoard = deliveryBoard(1);
duplicationBoard.delivery.id = "duplication-demo";
duplicationBoard.delivery.name = "UI duplication local verification";
duplicationBoard.tasks = [reviewTaskRow()];
for (const check of duplicationBoard.tasks[0].checks) {
  if (check.key === "verdict_ok") {
    check.status = "fail";
    check.failure_labels = ["Verdict pending: needs solver runs"];
    check.detail =
      "Insufficient evidence: no eligible solver trials for this task version.";
  } else if (check.key === "pre_trial_passed") {
    check.status = "fail";
    check.failure_labels = ["Pre-trial audit failed"];
    check.detail =
      "The environment could not install the test runner dependency.";
  } else if (check.key === "min_rollouts") {
    check.status = "fail";
    check.failure_labels = ["Runs: 2/5", "Agents: 1/3"];
    check.detail = "2/5 runs and 1/3 agents for verdict required.";
  }
}
export async function GET(request: NextRequest) {
  const parts = request.nextUrl.pathname
    .slice(5)
    .split("/")
    .map(decodeURIComponent);
  if (parts[0] === "duplication" && parts.at(-1) === "trials") {
    const original = tasks[0].trials![0];
    return NextResponse.json(
      ["source-a", "source-a", "source-b"].map((experiment_id, index) => ({
        ...original,
        id: `grouped-trial-${index}`,
        name: `grouped-trial-${index}`,
        experiment_id,
        analysis: {
          ...original.analysis,
          root_cause: "The runtime dependency is missing.",
          evidence:
            index === 0
              ? "The runtime dependency is missing."
              : "The process exited before the verifier started.",
        },
      }))
    );
  }
  if (parts[0] === "deliveries" && parts[1] === "duplication-demo") {
    if (parts[2] === "tasks")
      return NextResponse.json(duplicationBoard.tasks[0]);
    return NextResponse.json(
      pageFixture(duplicationBoard, request.nextUrl.searchParams)
    );
  }
  if (parts[0] === "deliveries" && parts[1] === "requirements-demo") {
    const examples = requirementExamples();
    return NextResponse.json(
      parts[2] === "selection"
        ? selectionFixture(examples, request.nextUrl.searchParams)
        : pageFixture(examples, request.nextUrl.searchParams)
    );
  }
  if (parts[0] === "deliveries") {
    if (parts[2] === "tasks") {
      const row = board.tasks.find((task) => task.task_id === parts[3]);
      return row
        ? NextResponse.json(row)
        : NextResponse.json(
            { detail: "Not a delivery member" },
            { status: 404 }
          );
    }
    return NextResponse.json(
      parts[2] === "selection"
        ? selectionFixture(board, request.nextUrl.searchParams)
        : pageFixture(board, request.nextUrl.searchParams)
    );
  }
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
export async function PUT(request: NextRequest) {
  // This fixture has no backend, credentials, queue, or paid operations.
  if (request.nextUrl.pathname === "/api/deliveries/duplication-demo/checks") {
    const { check_key, checked, expected_version_id } = await request.json();
    const task = duplicationBoard.tasks[0];
    if (expected_version_id !== task.version_id)
      return NextResponse.json({ detail: "Version changed" }, { status: 409 });
    if (check_key.startsWith("ack:")) {
      const finding = task.defects.find(
        (item) => `ack:${item.id}` === check_key
      );
      if (finding) {
        finding.acknowledged = checked;
        finding.acknowledged_by_name = "Local reviewer";
      }
    } else if (check_key.startsWith("waive:")) {
      const check = task.checks.find(
        (item) => `waive:${item.key}` === check_key
      );
      if (check) {
        check.status = checked ? "waived" : "fail";
        check.checked_by_name = "Local reviewer";
      }
    }
    return NextResponse.json({});
  }
  return NextResponse.json(
    { detail: "Mutation outside fixture scope" },
    { status: 405 }
  );
}
export async function POST(request: NextRequest) {
  if (/\/qa\/(retry|pre-trial)$/.test(request.nextUrl.pathname))
    return NextResponse.json({ status: "queued" });
  return NextResponse.json(
    { detail: "Mutation outside fixture scope" },
    { status: 405 }
  );
}
