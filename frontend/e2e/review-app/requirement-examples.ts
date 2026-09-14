import { board, taskRow } from "../delivery-fixtures";
import type { DeliveryCheckResult } from "@/lib/types";
import { tasks } from "./records";

const examples: { name: string; requirements: [string, string[]][] }[] = [
  {
    name: "Audit not started",
    requirements: [["pre_trial_passed", ["Pre-trial audit needed"]]],
  },
  {
    name: "Audit queued",
    requirements: [["pre_trial_passed", ["Pre-trial audit queued"]]],
  },
  {
    name: "Audit in progress",
    requirements: [["pre_trial_passed", ["Pre-trial audit running"]]],
  },
  {
    name: "Audit could not finish",
    requirements: [["pre_trial_passed", ["Pre-trial audit failed"]]],
  },
  {
    name: "More runs and agents needed",
    requirements: [
      ["min_rollouts", ["Runs: 2/5", "Agents: 1/3"]],
      ["verdict_ok", ["Verdict needed"]],
    ],
  },
  {
    name: "Enough runs, too few agents",
    requirements: [["min_rollouts", ["Agents: 1/3"]]],
  },
  {
    name: "Review not requested",
    requirements: [["verdict_ok", ["Verdict needed"]]],
  },
  {
    name: "Review in progress",
    requirements: [["verdict_ok", ["Verdict running"]]],
  },
  { name: "Three required fixes", requirements: [] },
];

export function requirementExamples() {
  const result = board();
  result.delivery.id = "requirements-demo";
  result.delivery.name = "Delivery requirement examples";
  result.tasks = examples.map((example, index) => {
    const task = tasks[index];
    const row = taskRow(task.current_version ?? 7);
    row.delivery_task_id = `requirement-example-${index}`;
    row.task_id = task.id;
    row.task_name = example.name;
    row.qa_work.owner_user_id = null;
    row.qa_owner_name = null;
    row.checks = example.requirements.map(
      ([key, failure_labels]): DeliveryCheckResult => ({
        key,
        failure_labels,
        kind: "automated",
        status: "fail",
        label: key,
        detail: "",
      })
    );
    if (index === 8) {
      row.qa.status = "needs_fixes";
      row.defects = [
        "Missing verifier check",
        "Incorrect expected result",
        "Missing dependency",
      ].map((title, n) => ({
        id: `example-fix-${n}`,
        title,
        source: "pre_trial",
        recorded_tier: "must_fix",
        acknowledged: false,
      }));
    }
    return row;
  });
  result.task_count = result.tasks.length;
  return result;
}
