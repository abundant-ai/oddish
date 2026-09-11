"use client";

import {
  Line,
  LineChart,
  ReferenceDot,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  DELIVERY_STATES,
  deliveryOwnerTasks,
  deliveryProgressHistory,
  deliveryTaskState,
  type DeliveryTaskState,
} from "@/lib/deliveries";
import type { DeliveryBoardResponse } from "@/lib/types";

const historyDate = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  timeZone: "UTC",
});

export function DeliveryOverview({
  board,
  ownerFilter,
}: {
  board: DeliveryBoardResponse;
  ownerFilter: string;
}) {
  const tasks = deliveryOwnerTasks(board, ownerFilter);
  const counts = {
    needs_work: 0,
    qa_incomplete: 0,
    awaiting_signoff: 0,
    ready: 0,
  };
  for (const row of tasks) counts[deliveryTaskState(row)]++;
  const days = deliveryProgressHistory(board, ownerFilter);
  const latest = days.findLast((day) => day.task_count !== null);
  const ownerName =
    ownerFilter === "all"
      ? "all owners"
      : ownerFilter === "mine"
        ? "my tasks"
        : ownerFilter === "unassigned"
          ? "unassigned tasks"
          : (tasks[0]?.qa_owner_name ?? ownerFilter);
  const closeEndpoints =
    latest &&
    latest.task_count! - latest.ready! <
      Math.max(1, ...days.map((day) => day.task_count ?? 0)) / 5;
  return (
    <section aria-label="Delivery overview" className="space-y-5">
      {!latest ? (
        <p className="text-muted-foreground py-3 text-sm">No history yet</p>
      ) : (
        <div
          className="flex h-52 min-w-0 flex-col sm:h-60"
          role="img"
          aria-label={`Progress for ${ownerName}: ${latest.ready} ready of ${latest.task_count} tasks on ${latest.date}.`}
        >
          <div className="min-h-0 flex-1">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart
                data={days}
                margin={{ top: 24, right: 96, bottom: 12, left: 8 }}
                accessibilityLayer
              >
                <XAxis dataKey="date" hide />
                <YAxis hide domain={[0, "dataMax"]} />
                <Tooltip
                  contentStyle={{
                    background: "hsl(var(--popover))",
                    color: "hsl(var(--popover-foreground))",
                    borderColor: "hsl(var(--border))",
                    borderRadius: 6,
                    fontSize: 12,
                  }}
                />
                <Line
                  name="Total"
                  dataKey="task_count"
                  type="stepAfter"
                  stroke="hsl(var(--muted-foreground))"
                  strokeWidth={1.5}
                  dot={({ cx, cy, index }) => (
                    <circle
                      key={index}
                      cx={cx}
                      cy={cy}
                      r={
                        days[index - 1]?.task_count == null &&
                        days[index + 1]?.task_count == null
                          ? 2
                          : 0
                      }
                      fill="hsl(var(--muted-foreground))"
                    />
                  )}
                  connectNulls={false}
                  isAnimationActive={false}
                />
                <Line
                  name="Ready"
                  dataKey="ready"
                  type="stepAfter"
                  stroke="var(--color-emerald-500)"
                  strokeWidth={2}
                  dot={({ cx, cy, index }) => (
                    <circle
                      key={index}
                      cx={cx}
                      cy={cy}
                      r={
                        days[index - 1]?.ready == null &&
                        days[index + 1]?.ready == null
                          ? 2
                          : 0
                      }
                      fill="var(--color-emerald-500)"
                    />
                  )}
                  connectNulls={false}
                  isAnimationActive={false}
                />
                <ReferenceDot
                  x={latest.date}
                  y={latest.task_count!}
                  r={0}
                  label={{
                    value: `${latest.task_count} total`,
                    position: "right",
                    dy: closeEndpoints ? (latest.ready === 0 ? -26 : -10) : 0,
                    fill: "hsl(var(--muted-foreground))",
                    fontSize: 12,
                  }}
                />
                <ReferenceDot
                  x={latest.date}
                  y={latest.ready!}
                  r={0}
                  label={{
                    value: `${latest.ready} ready`,
                    position: "right",
                    dy: closeEndpoints ? (latest.ready === 0 ? -8 : 10) : 0,
                    fill: "var(--color-emerald-500)",
                    fontSize: 12,
                  }}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
          <div className="text-muted-foreground flex justify-between pr-24 pl-2 text-xs">
            <time dateTime={days[0].date}>
              {historyDate.format(new Date(days[0].date))}
            </time>
            {days.length > 1 && (
              <time dateTime={days[days.length - 1].date}>
                {!board.frozen &&
                days[days.length - 1].date === board.qa_as_of?.slice(0, 10)
                  ? "Today"
                  : historyDate.format(new Date(days[days.length - 1].date))}
              </time>
            )}
          </div>
        </div>
      )}
      <dl
        className={`grid gap-4 py-4 ${counts.awaiting_signoff ? "grid-cols-2 sm:grid-cols-4" : "grid-cols-3"}`}
      >
        {(
          Object.entries(DELIVERY_STATES) as [
            DeliveryTaskState,
            (typeof DELIVERY_STATES)[DeliveryTaskState],
          ][]
        )
          .filter(([key]) => key !== "awaiting_signoff" || counts[key] > 0)
          .map(([key, state]) => (
            <div key={key}>
              <dt className="text-muted-foreground text-sm">{state.label}</dt>
              <dd className="mt-2 text-3xl font-medium tabular-nums">
                {counts[key]}
              </dd>
            </div>
          ))}
      </dl>
    </section>
  );
}
