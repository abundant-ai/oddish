import { expect, test } from "@playwright/test";

import { buildOddishRunCommand } from "../src/components/trial-detail-panel";
import type { Task, Trial } from "../src/lib/types";

test("preserves EC2 when copying a trial rerun command", () => {
  const command = buildOddishRunCommand(
    {
      agent: "nop",
      environment: "ec2",
      model: "nop_oracle",
      provider: "default",
      queue_key: "nop_oracle",
    } as Trial,
    {
      id: "task-1",
      experiment_id: "experiment-1",
    } as Task
  );

  expect(command).toBe(
    "oddish run --task task-1 --experiment experiment-1 -e ec2 -a nop -m nop_oracle"
  );
});

test("preserves Archil when copying a trial rerun command", () => {
  const command = buildOddishRunCommand(
    {
      agent: "nop",
      environment: "archil",
      model: "nop_oracle",
      provider: "default",
      queue_key: "nop_oracle",
    } as Trial,
    {
      id: "task-1",
      experiment_id: "experiment-1",
    } as Task
  );

  expect(command).toBe(
    "oddish run --task task-1 --experiment experiment-1 -e archil -a nop -m nop_oracle"
  );
});

test("does not prepend Bedrock to a canonical inference-profile model", () => {
  const command = buildOddishRunCommand(
    {
      agent: "claude-code",
      environment: "modal",
      model: "global.anthropic.claude-opus-5",
      provider: "bedrock",
      queue_key: "global.anthropic.claude-opus-5",
    } as Trial,
    {
      id: "task-1",
      experiment_id: "experiment-1",
    } as Task
  );

  expect(command).toBe(
    "oddish run --task task-1 --experiment experiment-1 -e modal -a claude-code -m global.anthropic.claude-opus-5"
  );
  expect(command).not.toContain("-m bedrock/");
});
