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
    } as Trial,
    {
      id: "task-1",
      experiment_id: "experiment-1",
    } as Task
  );

  expect(command).toBe(
    "oddish run --task task-1 --experiment experiment-1 -e ec2 -a nop -m default/nop_oracle"
  );
});
