"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Card, CardContent } from "@/components/ui/card";
import { isDeliveryBlocked, type DeliveryTaskFilter } from "@/lib/deliveries";
import type { DeliveryBoardResponse } from "@/lib/types";

const stages = [
  { key: "ready", label: "Ready", color: "#10b981" },
  { key: "awaiting_signoff", label: "Awaiting sign-off", color: "#f59e0b" },
  { key: "blocked", label: "Blocked", color: "#f87171" },
] as const;

export function DeliveryOverview({
  board,
  filter,
  ownerFilter,
  onFilter,
}: {
  board: DeliveryBoardResponse;
  filter: DeliveryTaskFilter;
  ownerFilter: string;
  onFilter: (filter: DeliveryTaskFilter, owner: string) => void;
}) {
  const counts = { ready: 0, blocked: 0, awaiting_signoff: 0, unassigned: 0 };
  const owners = new Map<
    string,
    { id: string; name: string; blocked: number; awaiting_signoff: number }
  >();
  let openFindings = 0;
  let acknowledgedFindings = 0;
  for (const row of board.tasks) {
    const stage = row.ready
      ? "ready"
      : isDeliveryBlocked(row)
        ? "blocked"
        : "awaiting_signoff";
    counts[stage]++;
    for (const finding of row.defects) {
      if (finding.acknowledged) acknowledgedFindings++;
      else openFindings++;
    }
    if (stage === "ready") continue;
    const id = row.qa_work.owner_user_id ?? "unassigned";
    if (id === "unassigned") counts.unassigned++;
    const owner = owners.get(id) ?? {
      id,
      name: row.qa_owner_name ?? row.qa_work.owner_user_id ?? "Unassigned",
      blocked: 0,
      awaiting_signoff: 0,
    };
    owner[stage]++;
    owners.set(id, owner);
  }
  const workload = [...owners.values()].sort(
    (a, b) =>
      b.blocked + b.awaiting_signoff - (a.blocked + a.awaiting_signoff) ||
      a.name.localeCompare(b.name)
  );
  const maxWork = Math.max(
    1,
    ...workload.map((owner) => owner.blocked + owner.awaiting_signoff)
  );
  const history = board.progress_history ?? [];
  // Missing dates have no bar, rather than implying zero tasks or interpolated progress.
  const byDay = new Map(
    history.map((point) => [point.recorded_at.slice(0, 10), point])
  );
  const days = [];
  if (history.length) {
    const start = new Date(history[0].recorded_at.slice(0, 10) + "T00:00:00Z");
    const end = new Date(
      (
        board.finalized_at ??
        board.qa_as_of ??
        history.at(-1)!.recorded_at
      ).slice(0, 10) + "T00:00:00Z"
    );
    for (
      let day = start;
      day <= end;
      day = new Date(day.getTime() + 86400000)
    ) {
      const date = day.toISOString().slice(0, 10);
      days.push({ date, ...byDay.get(date) });
    }
  }
  const latest = history.at(-1);
  return (
    <Card aria-label="Delivery overview">
      <CardContent className="space-y-5 pt-5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-sm font-medium">Delivery overview</h2>
          <button
            className="text-muted-foreground text-xs underline underline-offset-4"
            onClick={() => onFilter("all", "all")}
          >
            View all {board.task_count} tasks
          </button>
        </div>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          {[
            ...stages,
            { key: "unassigned", label: "Unassigned work", color: "#a1a1aa" },
          ].map((stage) => (
            <button
              key={stage.key}
              onClick={() =>
                onFilter(
                  stage.key === "unassigned"
                    ? "outstanding"
                    : (stage.key as DeliveryTaskFilter),
                  stage.key === "unassigned" ? "unassigned" : "all"
                )
              }
              aria-pressed={
                stage.key === "unassigned"
                  ? filter === "outstanding" && ownerFilter === "unassigned"
                  : filter === stage.key && ownerFilter === "all"
              }
              className="hover:bg-muted/60 aria-pressed:bg-muted rounded-md border px-3 py-2 text-left"
            >
              <span className="text-muted-foreground flex items-center gap-2 text-xs">
                <span
                  className="h-2 w-2 rounded-full"
                  style={{ backgroundColor: stage.color }}
                />
                {stage.label}
              </span>
              <span className="mt-1 block text-2xl font-medium tabular-nums">
                {counts[stage.key as keyof typeof counts]}
              </span>
            </button>
          ))}
        </div>
        <div className="grid gap-6 lg:grid-cols-[3fr_2fr]">
          <section className="min-w-0" aria-label="Delivery progress history">
            <h3 className="text-sm font-medium">Progress over time</h3>
            <p className="text-muted-foreground mt-1 text-xs">
              Last 30 days · latest hourly observation per day · UTC
            </p>
            {history.length ? (
              <>
                <div
                  className="mt-3 h-44"
                  role="img"
                  aria-label={`Delivery history, ${history.length} recorded days. Latest: ${latest!.ready} ready, ${latest!.blocked} blocked, ${latest!.awaiting_signoff} awaiting sign-off, ${latest!.task_count} total.`}
                >
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart
                      data={days}
                      margin={{ top: 4, right: 4, bottom: 0, left: -24 }}
                      accessibilityLayer
                    >
                      <CartesianGrid vertical={false} stroke="var(--border)" />
                      <XAxis
                        dataKey="date"
                        tickFormatter={(date: string) => date.slice(5)}
                        tick={{ fontSize: 11 }}
                        minTickGap={28}
                        axisLine={false}
                        tickLine={false}
                      />
                      <YAxis
                        allowDecimals={false}
                        tick={{ fontSize: 11 }}
                        axisLine={false}
                        tickLine={false}
                      />
                      <Tooltip
                        content={({ active, payload }) => {
                          const point = payload?.[0]?.payload;
                          return active && point?.recorded_at ? (
                            <div className="bg-popover text-popover-foreground rounded-md border p-3 text-xs shadow-md">
                              <p className="mb-2 font-medium">
                                {point.date} · {point.task_count} tasks
                              </p>
                              {stages.map((stage) => (
                                <p key={stage.key}>
                                  {stage.label}: {point[stage.key]}
                                </p>
                              ))}
                              <p className="mt-2">
                                Findings awaiting decision:{" "}
                                {point.open_findings}
                              </p>
                              <p>
                                Exceptions acknowledged:{" "}
                                {point.acknowledged_findings}
                              </p>
                            </div>
                          ) : null;
                        }}
                      />
                      {stages.map((stage) => (
                        <Bar
                          key={stage.key}
                          dataKey={stage.key}
                          name={stage.label}
                          stackId="status"
                          fill={stage.color}
                          maxBarSize={28}
                          isAnimationActive={false}
                        />
                      ))}
                    </BarChart>
                  </ResponsiveContainer>
                </div>
                <p className="text-muted-foreground mt-2 text-xs">
                  {history.length === 1
                    ? "History starts"
                    : "Showing history from"}{" "}
                  {history[0].recorded_at.slice(0, 10)}. Last recorded{" "}
                  {latest!.recorded_at.slice(0, 16).replace("T", " ")} UTC.
                  Missing days are gaps.
                </p>
              </>
            ) : (
              <div className="text-muted-foreground mt-3 flex h-44 items-center justify-center rounded-md border border-dashed px-6 text-center text-sm">
                {board.frozen
                  ? "No progress history was recorded before this delivery was finalized."
                  : "History starts with the first hourly observation. Current counts are shown above."}
              </div>
            )}
          </section>
          <section className="min-w-0" aria-label="Work by owner">
            <h3 className="text-sm font-medium">Outstanding work by owner</h3>
            <p className="text-muted-foreground mt-1 text-xs">
              Tasks in this delivery · select an owner to view their work
            </p>
            <div className="mt-3 max-h-52 space-y-1 overflow-y-auto">
              {workload.map((owner) => (
                <button
                  key={owner.id}
                  className="hover:bg-muted/60 aria-pressed:bg-muted w-full rounded-md px-2 py-2 text-left"
                  aria-label={`${owner.name}: ${owner.blocked} blocked, ${owner.awaiting_signoff} awaiting sign-off`}
                  aria-pressed={
                    ownerFilter === owner.id && filter === "outstanding"
                  }
                  onClick={() => onFilter("outstanding", owner.id)}
                >
                  <span className="mb-1.5 flex items-center justify-between gap-3 text-xs">
                    <span className="truncate">{owner.name}</span>
                    <span className="tabular-nums">
                      {owner.blocked + owner.awaiting_signoff}
                    </span>
                  </span>
                  <span className="bg-muted flex h-2 overflow-hidden rounded-full">
                    <span
                      className="bg-red-400"
                      style={{ width: `${(100 * owner.blocked) / maxWork}%` }}
                    />
                    <span
                      className="bg-amber-500"
                      style={{
                        width: `${(100 * owner.awaiting_signoff) / maxWork}%`,
                      }}
                    />
                  </span>
                </button>
              ))}
              {!workload.length && (
                <p className="text-muted-foreground py-8 text-center text-sm">
                  No outstanding work.
                </p>
              )}
            </div>
          </section>
        </div>
        <p className="text-muted-foreground border-t pt-3 text-xs">
          {openFindings} findings awaiting a decision · {acknowledgedFindings}{" "}
          acknowledged as exceptions.
          {board.delivery_checks.some((check) => check.status === "fail") &&
            " Delivery-wide checks also remain open."}
        </p>
      </CardContent>
    </Card>
  );
}
