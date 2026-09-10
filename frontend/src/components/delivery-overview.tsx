"use client";

import {
  CartesianGrid,
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
  type DeliveryTaskFilter,
  type DeliveryTaskState,
} from "@/lib/deliveries";
import type { DeliveryBoardResponse } from "@/lib/types";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

export function DeliveryOverview({
  board,
  filter,
  ownerFilter,
  onFilter,
  onOwnerChange,
}: {
  board: DeliveryBoardResponse;
  filter: DeliveryTaskFilter;
  ownerFilter: string;
  onFilter: (filter: DeliveryTaskFilter) => void;
  onOwnerChange: (owner: string) => void;
}) {
  const tasks = deliveryOwnerTasks(board, ownerFilter);
  const counts = {
    needs_work: 0,
    qa_incomplete: 0,
    awaiting_signoff: 0,
    ready: 0,
  };
  for (const row of tasks) counts[deliveryTaskState(row)]++;
  const owners = new Map<string, string>();
  for (const row of board.tasks) {
    if (row.qa_work.owner_user_id) {
      owners.set(
        row.qa_work.owner_user_id,
        row.qa_owner_name ?? row.qa_work.owner_user_id
      );
    }
  }
  const days = deliveryProgressHistory(board, ownerFilter);
  const latest = days.findLast((day) => day.task_count !== null);
  const closeEndpoints =
    latest &&
    latest.task_count! - latest.ready! <
      Math.max(1, ...days.map((day) => day.task_count ?? 0)) / 5;
  return (
    <section aria-label="Delivery overview" className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <span className="text-sm font-medium">
          {tasks.length} task{tasks.length === 1 ? "" : "s"}
        </span>
        <div className="flex items-center gap-2">
          <label
            htmlFor="delivery-owner"
            className="text-muted-foreground text-xs"
          >
            Owner
          </label>
          <Select value={ownerFilter} onValueChange={onOwnerChange}>
            <SelectTrigger
              id="delivery-owner"
              className="w-44"
              aria-label="Owner filter"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All owners</SelectItem>
              {board.qa_viewer_user_id && (
                <SelectItem value="mine">Mine</SelectItem>
              )}
              <SelectItem value="unassigned">Unassigned</SelectItem>
              {[...owners]
                .sort((a, b) => a[1].localeCompare(b[1]))
                .map(([id, name]) => (
                  <SelectItem key={id} value={id}>
                    {name}
                  </SelectItem>
                ))}
              {!["all", "mine", "unassigned"].includes(ownerFilter) &&
                !owners.has(ownerFilter) && (
                  <SelectItem value={ownerFilter}>{ownerFilter}</SelectItem>
                )}
            </SelectContent>
          </Select>
        </div>
      </div>
      <div className="grid grid-cols-2 border-y sm:grid-cols-4">
        {(
          Object.entries(DELIVERY_STATES) as [
            DeliveryTaskState,
            (typeof DELIVERY_STATES)[DeliveryTaskState],
          ][]
        ).map(([key, state]) => (
          <button
            type="button"
            key={key}
            onClick={() => onFilter(filter === key ? "all" : key)}
            aria-pressed={filter === key}
            className={`hover:bg-muted/50 aria-pressed:bg-muted/50 border-b-2 border-transparent px-4 py-4 text-left aria-pressed:border-current ${state.tone}`}
          >
            <span className="text-muted-foreground flex items-center gap-2 text-xs">
              <span
                className={`h-1.5 w-1.5 rounded-full bg-current ${state.tone}`}
                aria-hidden="true"
              />
              {state.label}
            </span>
            <span className="text-foreground mt-2 block text-3xl font-medium tabular-nums">
              {counts[key]}
            </span>
          </button>
        ))}
      </div>
      {latest ? (
        <div
          className="h-44"
          role="img"
          aria-label={`Progress for ${ownerFilter === "all" ? "all owners" : ownerFilter === "mine" ? "my tasks" : (owners.get(ownerFilter) ?? ownerFilter)}: ${latest.ready} ready of ${latest.task_count} tasks on ${latest.date}.`}
        >
          <ResponsiveContainer width="100%" height="100%">
            <LineChart
              data={days}
              margin={{ top: 16, right: 60, bottom: 0, left: -24 }}
              accessibilityLayer
            >
              <CartesianGrid vertical={false} stroke="hsl(var(--border))" />
              <XAxis
                dataKey="date"
                tickFormatter={(date: string) => date.slice(5)}
                tick={{ fontSize: 11, fill: "hsl(var(--muted-foreground))" }}
                minTickGap={40}
                axisLine={false}
                tickLine={false}
              />
              <YAxis
                allowDecimals={false}
                tick={{ fontSize: 11, fill: "hsl(var(--muted-foreground))" }}
                axisLine={false}
                tickLine={false}
              />
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
                dot={{ r: 2, fill: "hsl(var(--muted-foreground))" }}
                connectNulls={false}
                isAnimationActive={false}
              />
              <Line
                name="Ready"
                dataKey="ready"
                type="stepAfter"
                stroke="var(--color-emerald-500)"
                strokeWidth={2}
                dot={{ r: 2, fill: "var(--color-emerald-500)" }}
                connectNulls={false}
                isAnimationActive={false}
              />
              <ReferenceDot
                x={latest.date}
                y={latest.task_count!}
                r={0}
                label={{
                  value: "Total",
                  position: "right",
                  dy: closeEndpoints ? -9 : 0,
                  fill: "hsl(var(--muted-foreground))",
                  fontSize: 12,
                }}
              />
              <ReferenceDot
                x={latest.date}
                y={latest.ready!}
                r={0}
                label={{
                  value: "Ready",
                  position: "right",
                  dy: closeEndpoints ? (latest.ready === 0 ? -24 : 9) : 0,
                  fill: "var(--color-emerald-500)",
                  fontSize: 12,
                }}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      ) : (
        <p className="text-muted-foreground py-3 text-sm">No history yet</p>
      )}
    </section>
  );
}
