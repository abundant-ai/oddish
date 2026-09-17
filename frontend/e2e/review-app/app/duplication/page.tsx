"use client";

import { Suspense, useState } from "react";
import {
  UsageOverviewCard,
  type TimeRangeKey,
} from "@/components/usage-overview";
import { QaAssessmentReport } from "@/components/qa-report/qa-assessment-report";
import { TaskOverviewPanel } from "@/components/task-overview-panel";
import { tasks, versionFor } from "../../records";

export default function DuplicationPreview() {
  const [timeRange, setTimeRange] = useState<TimeRangeKey>("30d");
  const [count, setCount] = useState(2);
  const findings = versionFor(tasks[0], 7).pre_trial_findings ?? [];
  return (
    <div className="mx-auto max-w-5xl space-y-8">
      <h1 className="text-xl font-semibold">UI duplication verification</h1>
      <p className="text-muted-foreground text-sm">
        Production components with local fixture data. No live jobs or delivery
        records are modified.
      </p>
      <UsageOverviewCard
        queues={null}
        modelUsage={[
          {
            model: "openai/gpt-5.6",
            provider: "openai",
            trial_count: 6,
            input_tokens: 1200000,
            output_tokens: 300000,
            cache_tokens: 200000,
            total_steps: 30,
            cost_usd: 4.5,
            running: 0,
            queued: 0,
            succeeded: 6,
            failed: 0,
            avg_duration_s: 45,
          },
        ]}
        jobUsage={[]}
        error={undefined}
        isLoading={false}
        isRefreshing={false}
        timeRange={timeRange}
        onTimeRangeChange={setTimeRange}
      />
      <section className="space-y-3">
        <label className="text-sm">
          Defect count{" "}
          <select
            aria-label="Defect count"
            value={count}
            onChange={(event) => setCount(Number(event.target.value))}
          >
            <option value={0}>0</option>
            <option value={1}>1</option>
            <option value={2}>2</option>
          </select>
        </label>
        <QaAssessmentReport
          classification="BAD_FAILURE"
          subtype="Environment Defect"
          rootCause="The task environment lacks the dependency used by the test runner."
          recommendation="Install the dependency before running the verifier."
          actionItems={Array.from({ length: count }, (_, index) => ({
            ...findings[0],
            id: `defect-${index}`,
            title: index
              ? "Test runner cannot start"
              : "Missing runtime dependency",
          }))}
          onFeedback={async () => {}}
        />
      </section>
      <Suspense fallback={<p>Loading task review…</p>}>
        <TaskOverviewPanel
          taskId="task-a"
          apiBaseUrl="/api/duplication"
          version={7}
          scopeTrials={[]}
          verdictTask={tasks[0]}
          checksFindings={[]}
          checksStatus="success"
          experiments={[
            { id: "source-a", name: "Dependency baseline" },
            { id: "source-b", name: "Verifier comparison" },
          ]}
          onRerunChecks={() => {}}
          checksRerunning={false}
        />
      </Suspense>
    </div>
  );
}
