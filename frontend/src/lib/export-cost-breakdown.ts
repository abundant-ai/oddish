import type {
  CostExperimentBreakdown,
  CostModelBreakdown,
  CostUserBreakdown,
} from "@/lib/types";

function topModelsLabel(models: CostModelBreakdown[], n = 3): string {
  return [...models]
    .sort((a, b) => b.cost_usd - a.cost_usd)
    .slice(0, n)
    .map((m) => m.model)
    .join(", ");
}

// Round to cents and emit a real number so Excel can sum/sort it.
function money(n: number): number {
  return Math.round((n + Number.EPSILON) * 100) / 100;
}

type SheetRow = Record<string, string | number>;

// Size each column to its widest cell (header included), capped so the long
// "Top models" / token columns don't blow out. SheetJS CE supports column
// widths (!cols) and autofilter, but not cell styling (fills/bold/color).
function autoCols(rows: SheetRow[]): { wch: number }[] {
  if (rows.length === 0) return [];
  return Object.keys(rows[0]).map((key) => {
    let max = key.length;
    for (const row of rows) {
      const len = String(row[key] ?? "").length;
      if (len > max) max = len;
    }
    return { wch: Math.min(Math.max(max + 2, 8), 50) };
  });
}

function makeSheet(XLSX: typeof import("xlsx"), rows: SheetRow[]) {
  const ws = XLSX.utils.json_to_sheet(rows);
  ws["!cols"] = autoCols(rows);
  if (ws["!ref"]) ws["!autofilter"] = { ref: ws["!ref"] };
  return ws;
}

/**
 * Build and download a 3-sheet .xlsx (Cost by user / Cost by model / Top
 * experiments by cost) from the *already-filtered* cost breakdown rows — i.e.
 * whatever the current search produced, across all pages. SheetJS is loaded
 * lazily so it stays out of the initial bundle.
 */
export async function exportCostBreakdownXlsx({
  users,
  models,
  experiments,
  windowDays,
}: {
  users: CostUserBreakdown[];
  models: CostModelBreakdown[];
  experiments: CostExperimentBreakdown[];
  windowDays: string;
}): Promise<void> {
  const XLSX = await import("xlsx");

  const userRows = users.map((u) => ({
    User: u.name || u.email || u.owner_user_id || "Unattributed",
    Email: u.email ?? "",
    Org: u.org_name ?? u.org_id ?? "",
    "Cost (USD)": money(u.cost_usd),
    "Estimated (USD)": money(u.cost_estimated_usd),
    Trials: u.trial_count,
    Experiments: u.experiment_count,
    "Input tokens": u.input_tokens,
    "Cache tokens": u.cache_tokens,
    "Output tokens": u.output_tokens,
    "Top models": topModelsLabel(u.models),
  }));

  const modelRows = models.map((m) => ({
    Model: m.model,
    Provider: m.provider,
    "Cost (USD)": money(m.cost_usd),
    "Estimated (USD)": money(m.cost_estimated_usd),
    Trials: m.trial_count,
    "Input tokens": m.input_tokens,
    "Cache tokens": m.cache_tokens,
    "Output tokens": m.output_tokens,
  }));

  const experimentRows = experiments.map((e) => ({
    Experiment: e.name ?? e.experiment_id,
    Owner: e.owner_name || e.owner_email || e.owner_user_id || "Unattributed",
    Org: e.org_name ?? e.org_id ?? "",
    "Cost (USD)": money(e.cost_usd),
    "Estimated (USD)": money(e.cost_estimated_usd),
    Trials: e.trial_count,
    "Input tokens": e.input_tokens,
    "Cache tokens": e.cache_tokens,
    "Output tokens": e.output_tokens,
    Created: e.created_at ?? "",
    "Last activity": e.last_activity_at ?? "",
    "Top models": topModelsLabel(e.models),
  }));

  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, makeSheet(XLSX, userRows), "Cost by user");
  XLSX.utils.book_append_sheet(wb, makeSheet(XLSX, modelRows), "Cost by model");
  XLSX.utils.book_append_sheet(
    wb,
    makeSheet(XLSX, experimentRows),
    "Top experiments by cost",
  );

  const windowLabel = windowDays === "0" ? "all-time" : `${windowDays}d`;
  const date = new Date().toISOString().slice(0, 10);
  XLSX.writeFile(wb, `cost-breakdown-${windowLabel}-${date}.xlsx`);
}
