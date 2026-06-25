"use client";

import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Loader2, Search } from "lucide-react";
import type { CostBreakdownRow } from "@/lib/cost-breakdown";
import {
  TimeRangePicker,
  type TimeRangeKey,
} from "@/components/cost/time-range-picker";

// ---------------------------------------------------------------------------
// Dimensions
// ---------------------------------------------------------------------------

const DIMS = { model: "Model", experiment: "Experiment", user: "User" } as const;
type Dim = keyof typeof DIMS;
const DIM_KEYS = Object.keys(DIMS) as Dim[];

// Stable grouping key vs. human label. Experiments group by id (names can
// collide); users group by owner id.
function dimKey(row: CostBreakdownRow, dim: Dim): string {
  if (dim === "model") return row.model;
  if (dim === "experiment") return row.experiment_id ?? "__other__";
  return row.owner_user_id ?? "__unassigned__";
}
function dimLabel(row: CostBreakdownRow, dim: Dim): string {
  if (dim === "model") return row.model;
  if (dim === "experiment") return row.experiment_name;
  return row.owner_label;
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

function formatCost(usd: number): string {
  if (usd >= 100) return `$${Math.round(usd).toLocaleString()}`;
  if (usd >= 1) return `$${usd.toFixed(2)}`;
  if (usd >= 0.01) return `$${usd.toFixed(3)}`;
  if (usd > 0) return `$${usd.toFixed(4)}`;
  return "$0";
}

const PROVIDER_COLOR: Record<string, string> = {
  openai: "#1d9e75",
  google: "#378add",
  gemini: "#378add",
  anthropic: "#d85a30",
};
const ACCENT = "#6f88b4";

const TOP_CAP = 50;
const CHILD_CAP = 25;

// ---------------------------------------------------------------------------
// Aggregation
// ---------------------------------------------------------------------------

type Leaf = {
  key: string;
  primaryKey: string;
  primaryLabel: string;
  secondaryKey: string | null;
  secondaryLabel: string | null;
  cost: number;
  trials: number;
  active: number;
  provider: string | null; // single provider if consistent, else null
};

function aggregate(rows: CostBreakdownRow[], dims: Dim[]): Leaf[] {
  const map = new Map<string, Leaf & { providers: Set<string> }>();
  for (const row of rows) {
    const pKey = dimKey(row, dims[0]);
    const sKey = dims[1] ? dimKey(row, dims[1]) : null;
    const key = sKey == null ? pKey : `${pKey}${sKey}`;
    let leaf = map.get(key);
    if (!leaf) {
      leaf = {
        key,
        primaryKey: pKey,
        primaryLabel: dimLabel(row, dims[0]),
        secondaryKey: sKey,
        secondaryLabel: dims[1] ? dimLabel(row, dims[1]) : null,
        cost: 0,
        trials: 0,
        active: 0,
        provider: null,
        providers: new Set<string>(),
      };
      map.set(key, leaf);
    }
    leaf.cost += row.cost_usd;
    leaf.trials += row.trial_count;
    leaf.active += row.running + row.queued + row.retrying;
    if (row.provider) leaf.providers.add(row.provider);
  }
  return Array.from(map.values()).map((l) => ({
    ...l,
    provider: l.providers.size === 1 ? [...l.providers][0] : null,
  }));
}

function leafColor(leaf: { provider: string | null }): string {
  return (leaf.provider && PROVIDER_COLOR[leaf.provider]) || ACCENT;
}

// ---------------------------------------------------------------------------
// Small presentational helpers
// ---------------------------------------------------------------------------

function CostBar({
  pct,
  color,
  value,
  bold,
}: {
  pct: number;
  color: string;
  value: number;
  bold?: boolean;
}) {
  return (
    <div className="flex items-center justify-end gap-2">
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
        <div
          className="h-full rounded-full"
          style={{ width: `${pct}%`, background: color }}
        />
      </div>
      <span
        className={`min-w-[62px] text-right font-mono text-xs tabular-nums ${
          bold ? "font-medium" : ""
        }`}
      >
        {formatCost(value)}
      </span>
    </div>
  );
}

function GroupByPills({
  primary,
  secondary,
  onPrimary,
  onSecondary,
}: {
  primary: Dim;
  secondary: Dim | "none";
  onPrimary: (d: Dim) => void;
  onSecondary: (d: Dim | "none") => void;
}) {
  const pill = (active: boolean, label: string, onClick: () => void) => (
    <Button
      key={label}
      type="button"
      variant={active ? "secondary" : "ghost"}
      size="sm"
      className={`h-7 px-2.5 text-[11px] ${
        active ? "border border-[#85b85c]/30" : "border border-transparent"
      }`}
      onClick={onClick}
    >
      {label}
    </Button>
  );
  return (
    <div className="flex flex-col gap-1">
      <span className="text-[10px] text-muted-foreground">Group by</span>
      <div className="flex items-center gap-1">
        <div className="flex gap-0.5">
          {DIM_KEYS.map((d) => pill(primary === d, DIMS[d], () => onPrimary(d)))}
        </div>
        <span className="px-1 text-muted-foreground">›</span>
        <div className="flex gap-0.5">
          {pill(secondary === "none", "None", () => onSecondary("none"))}
          {DIM_KEYS.filter((d) => d !== primary).map((d) =>
            pill(secondary === d, DIMS[d], () => onSecondary(d)),
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// View
// ---------------------------------------------------------------------------

export function CostBreakdownView({
  rows,
  error,
  isLoading,
  isRefreshing,
  timeRange,
  onTimeRangeChange,
}: {
  rows: CostBreakdownRow[];
  error?: Error;
  isLoading: boolean;
  isRefreshing: boolean;
  timeRange: TimeRangeKey;
  onTimeRangeChange: (key: TimeRangeKey) => void;
}) {
  const [primary, setPrimary] = useState<Dim>("experiment");
  const [secondary, setSecondary] = useState<Dim | "none">("model");
  const [layout, setLayout] = useState<"nested" | "matrix">("nested");
  const [search, setSearch] = useState("");
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const dims: Dim[] = secondary === "none" ? [primary] : [primary, secondary];
  const leaves = useMemo(() => aggregate(rows, dims), [rows, dims]);

  const q = search.trim().toLowerCase();
  const filtered = useMemo(() => {
    if (!q) return leaves;
    return leaves.filter(
      (l) =>
        l.primaryLabel.toLowerCase().includes(q) ||
        (l.secondaryLabel?.toLowerCase().includes(q) ?? false),
    );
  }, [leaves, q]);

  const totals = useMemo(
    () =>
      filtered.reduce(
        (acc, l) => ({
          cost: acc.cost + l.cost,
          trials: acc.trials + l.trials,
          active: acc.active + l.active,
        }),
        { cost: 0, trials: 0, active: 0 },
      ),
    [filtered],
  );

  const setPrimaryDim = (d: Dim) => {
    setPrimary(d);
    if (secondary === d) setSecondary("none");
    setExpanded({});
  };
  const setSecondaryDim = (d: Dim | "none") => {
    setSecondary(d);
    if (d === "none") setLayout("nested");
    setExpanded({});
  };
  const toggle = (key: string) =>
    setExpanded((prev) => ({ ...prev, [key]: !prev[key] }));

  const twoDim = secondary !== "none";

  return (
    <Card className="border-[#6f88b4]/20 shadow-xs">
      <CardHeader className="pb-2">
        <div className="flex flex-wrap items-center gap-2">
          <CardTitle className="text-base">Cost breakdown</CardTitle>
          {(isLoading || isRefreshing) && (
            <Badge
              variant="outline"
              className="text-[10px] font-normal text-muted-foreground"
            >
              <Loader2 className="mr-1 h-3 w-3 animate-spin" />
              {isLoading ? "Loading" : "Updating"}
            </Badge>
          )}
          <div className="ml-auto">
            <TimeRangePicker value={timeRange} onChange={onTimeRangeChange} />
          </div>
        </div>

        <div className="mt-2 flex flex-wrap items-end gap-3">
          <GroupByPills
            primary={primary}
            secondary={secondary}
            onPrimary={setPrimaryDim}
            onSecondary={setSecondaryDim}
          />
          {twoDim && (
            <div className="flex flex-col gap-1">
              <span className="text-[10px] text-muted-foreground">Layout</span>
              <div className="flex gap-0.5">
                {(["nested", "matrix"] as const).map((l) => (
                  <Button
                    key={l}
                    type="button"
                    variant={layout === l ? "secondary" : "ghost"}
                    size="sm"
                    className={`h-7 px-2.5 text-[11px] capitalize ${
                      layout === l
                        ? "border border-[#85b85c]/30"
                        : "border border-transparent"
                    }`}
                    onClick={() => setLayout(l)}
                  >
                    {l}
                  </Button>
                ))}
              </div>
            </div>
          )}
          <div className="ml-auto flex flex-col gap-1">
            <span className="text-[10px] text-muted-foreground">Search</span>
            <div className="relative">
              <Search className="absolute top-1/2 left-2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Filter…"
                className="h-7 w-[180px] pl-7 text-[11px]"
                aria-label="Filter results"
              />
            </div>
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-3">
        <div className="grid grid-cols-3 gap-2">
          {[
            ["Total cost", formatCost(totals.cost)],
            ["Trials", totals.trials.toLocaleString()],
            ["Active now", String(totals.active)],
          ].map(([label, value]) => (
            <div
              key={label}
              className="rounded-md border border-[#6f88b4]/18 bg-background/70 p-2 text-center"
            >
              <div className="text-base font-bold tabular-nums">{value}</div>
              <div className="text-[10px] text-muted-foreground">{label}</div>
            </div>
          ))}
        </div>

        {error ? (
          <Alert variant="destructive">
            <AlertTitle>Cost breakdown unavailable</AlertTitle>
            <AlertDescription>Failed to load cost data.</AlertDescription>
          </Alert>
        ) : isLoading && rows.length === 0 ? (
          <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            Loading cost data…
          </div>
        ) : filtered.length === 0 ? (
          <div className="py-6 text-center text-sm text-muted-foreground">
            {rows.length === 0
              ? "No trial cost data in this window yet."
              : "No results match your search."}
          </div>
        ) : twoDim && layout === "matrix" ? (
          <MatrixView
            leaves={filtered}
            primaryLabel={DIMS[primary]}
            secondaryLabel={DIMS[secondary as Dim]}
          />
        ) : twoDim ? (
          <NestedView
            leaves={filtered}
            primary={DIMS[primary]}
            secondary={DIMS[secondary as Dim]}
            expanded={expanded}
            onToggle={toggle}
          />
        ) : (
          <SingleView
            leaves={filtered}
            label={DIMS[primary]}
            expanded={expanded}
            onToggle={toggle}
          />
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Single-dimension ranked table
// ---------------------------------------------------------------------------

function SingleView({
  leaves,
  label,
  expanded,
  onToggle,
}: {
  leaves: Leaf[];
  label: string;
  expanded: Record<string, boolean>;
  onToggle: (key: string) => void;
}) {
  const sorted = [...leaves].sort((a, b) => b.cost - a.cost);
  const max = sorted[0]?.cost || 1;
  const showAll = expanded["__top__"];
  const visible = showAll ? sorted : sorted.slice(0, TOP_CAP);
  const rest = sorted.slice(visible.length);
  const restCost = rest.reduce((s, l) => s + l.cost, 0);

  return (
    <div className="overflow-x-auto">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>{label}</TableHead>
            <TableHead className="text-right">Trials</TableHead>
            <TableHead className="w-[200px] text-right">Cost</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {visible.map((l) => (
            <TableRow key={l.key}>
              <TableCell className="font-mono text-xs">{l.primaryLabel}</TableCell>
              <TableCell className="text-right font-mono text-xs text-muted-foreground">
                {l.trials}
              </TableCell>
              <TableCell>
                <CostBar pct={(l.cost / max) * 100} color={leafColor(l)} value={l.cost} />
              </TableCell>
            </TableRow>
          ))}
          {rest.length > 0 && (
            <TailRow
              count={sorted.length}
              restCount={rest.length}
              restCost={restCost}
              onShowAll={() => onToggle("__top__")}
            />
          )}
        </TableBody>
      </Table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Two-dimension nested table
// ---------------------------------------------------------------------------

type Group = {
  key: string;
  label: string;
  cost: number;
  trials: number;
  provider: string | null;
  kids: Leaf[];
};

function NestedView({
  leaves,
  primary,
  secondary,
  expanded,
  onToggle,
}: {
  leaves: Leaf[];
  primary: string;
  secondary: string;
  expanded: Record<string, boolean>;
  onToggle: (key: string) => void;
}) {
  const groups = new Map<string, Group>();
  for (const l of leaves) {
    let g = groups.get(l.primaryKey);
    if (!g) {
      g = {
        key: l.primaryKey,
        label: l.primaryLabel,
        cost: 0,
        trials: 0,
        provider: l.provider,
        kids: [],
      };
      groups.set(l.primaryKey, g);
    }
    g.cost += l.cost;
    g.trials += l.trials;
    if (g.provider !== l.provider) g.provider = g.provider ?? l.provider;
    g.kids.push(l);
  }
  const garr = [...groups.values()].sort((a, b) => b.cost - a.cost);
  const gmax = garr[0]?.cost || 1;
  const showAllGroups = expanded["__top__"];
  const visibleGroups = showAllGroups ? garr : garr.slice(0, TOP_CAP);
  const restGroups = garr.slice(visibleGroups.length);
  const restGroupsCost = restGroups.reduce((s, g) => s + g.cost, 0);

  return (
    <div className="overflow-x-auto">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>
              {primary} › {secondary}
            </TableHead>
            <TableHead className="text-right">Trials</TableHead>
            <TableHead className="w-[200px] text-right">Cost</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {visibleGroups.map((g) => {
            const color = leafColor(g);
            const kids = [...g.kids].sort((a, b) => b.cost - a.cost);
            const kmax = kids[0]?.cost || 1;
            const childKey = `child:${g.key}`;
            const showAllKids = expanded[childKey];
            const visibleKids = showAllKids ? kids : kids.slice(0, CHILD_CAP);
            const restKids = kids.slice(visibleKids.length);
            const restKidsCost = restKids.reduce((s, k) => s + k.cost, 0);
            return (
              <RowGroup key={g.key}>
                <TableRow className="bg-muted/40">
                  <TableCell className="font-mono text-xs font-medium">
                    {g.label}
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs text-muted-foreground">
                    {g.trials}
                  </TableCell>
                  <TableCell>
                    <CostBar pct={(g.cost / gmax) * 100} color={color} value={g.cost} bold />
                  </TableCell>
                </TableRow>
                {visibleKids.map((k) => (
                  <TableRow key={k.key}>
                    <TableCell className="py-1.5 pl-7 font-mono text-xs text-muted-foreground">
                      {k.secondaryLabel}
                    </TableCell>
                    <TableCell className="py-1.5 text-right font-mono text-xs text-muted-foreground/70">
                      {k.trials}
                    </TableCell>
                    <TableCell className="py-1.5">
                      <CostBar pct={(k.cost / kmax) * 100} color={`${color}99`} value={k.cost} />
                    </TableCell>
                  </TableRow>
                ))}
                {restKids.length > 0 && (
                  <TailRow
                    indent
                    count={kids.length}
                    restCount={restKids.length}
                    restCost={restKidsCost}
                    onShowAll={() => onToggle(childKey)}
                  />
                )}
              </RowGroup>
            );
          })}
          {restGroups.length > 0 && (
            <TailRow
              count={garr.length}
              restCount={restGroups.length}
              restCost={restGroupsCost}
              onShowAll={() => onToggle("__top__")}
            />
          )}
        </TableBody>
      </Table>
    </div>
  );
}

function RowGroup({ children }: { children: ReactNode }) {
  return <>{children}</>;
}

function TailRow({
  count,
  restCount,
  restCost,
  onShowAll,
  indent,
}: {
  count: number;
  restCount: number;
  restCost: number;
  onShowAll: () => void;
  indent?: boolean;
}) {
  return (
    <TableRow>
      <TableCell colSpan={3} className={indent ? "pl-7" : undefined}>
        <div className="flex items-center gap-3">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-6 px-2 text-[11px] text-muted-foreground"
            onClick={onShowAll}
          >
            Show all {count} ›
          </Button>
          <span className="text-[11px] text-muted-foreground">
            Other ({restCount}) — {formatCost(restCost)}
          </span>
        </div>
      </TableCell>
    </TableRow>
  );
}

// ---------------------------------------------------------------------------
// Two-dimension pivot matrix
// ---------------------------------------------------------------------------

const MATRIX_ROW_CAP = 12;
const MATRIX_COL_CAP = 10;

function MatrixView({
  leaves,
  primaryLabel,
  secondaryLabel,
}: {
  leaves: Leaf[];
  primaryLabel: string;
  secondaryLabel: string;
}) {
  const rowTotals = new Map<string, { label: string; cost: number }>();
  const colTotals = new Map<string, { label: string; cost: number }>();
  const cell = new Map<string, number>();
  let max = 0;
  let grand = 0;
  for (const l of leaves) {
    const sKey = l.secondaryKey ?? "—";
    const sLabel = l.secondaryLabel ?? "—";
    rowTotals.set(l.primaryKey, {
      label: l.primaryLabel,
      cost: (rowTotals.get(l.primaryKey)?.cost ?? 0) + l.cost,
    });
    colTotals.set(sKey, {
      label: sLabel,
      cost: (colTotals.get(sKey)?.cost ?? 0) + l.cost,
    });
    const ck = `${l.primaryKey}${sKey}`;
    const v = (cell.get(ck) ?? 0) + l.cost;
    cell.set(ck, v);
    if (v > max) max = v;
    grand += l.cost;
  }
  const rows = [...rowTotals.entries()]
    .sort((a, b) => b[1].cost - a[1].cost)
    .slice(0, MATRIX_ROW_CAP);
  const cols = [...colTotals.entries()]
    .sort((a, b) => b[1].cost - a[1].cost)
    .slice(0, MATRIX_COL_CAP);

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[520px] border-collapse text-xs">
        <thead>
          <tr className="border-b border-[#6f88b4]/20">
            <th className="bg-background sticky left-0 px-2.5 py-2 text-left font-medium text-muted-foreground">
              {primaryLabel} \ {secondaryLabel}
            </th>
            {cols.map(([k, c]) => (
              <th
                key={k}
                className="px-2.5 py-2 text-right font-medium whitespace-nowrap text-muted-foreground"
                title={c.label}
              >
                {c.label}
              </th>
            ))}
            <th className="border-l border-[#6f88b4]/20 px-2.5 py-2 text-right font-medium">
              Total
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([rk, r]) => (
            <tr key={rk} className="border-t border-[#6f88b4]/10">
              <td
                className="bg-background sticky left-0 px-2.5 py-1.5 font-mono"
                title={r.label}
              >
                {r.label}
              </td>
              {cols.map(([ck]) => {
                const v = cell.get(`${rk}${ck}`) ?? 0;
                const a = v ? 0.08 + 0.55 * (v / max) : 0;
                return (
                  <td
                    key={ck}
                    className="px-2.5 py-1.5 text-right font-mono tabular-nums"
                    style={{
                      background: v ? `rgba(111,136,180,${a.toFixed(3)})` : undefined,
                    }}
                  >
                    {v ? formatCost(v) : ""}
                  </td>
                );
              })}
              <td className="border-l border-[#6f88b4]/20 px-2.5 py-1.5 text-right font-mono font-medium tabular-nums">
                {formatCost(r.cost)}
              </td>
            </tr>
          ))}
          <tr className="border-t border-[#6f88b4]/20 bg-muted/40">
            <td className="bg-muted/40 sticky left-0 px-2.5 py-2 font-medium">
              Total
            </td>
            {cols.map(([ck, c]) => (
              <td
                key={ck}
                className="px-2.5 py-2 text-right font-mono font-medium tabular-nums"
              >
                {formatCost(c.cost)}
              </td>
            ))}
            <td className="border-l border-[#6f88b4]/20 px-2.5 py-2 text-right font-mono font-medium tabular-nums">
              {formatCost(grand)}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}
