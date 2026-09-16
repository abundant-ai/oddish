import type { CostSeries } from "@/lib/types";

export type SpendGroupId = "models" | "compute";

export type SpendOptionId = `model:${string}` | `compute:${string}`;

export type SpendOption = {
  id: SpendOptionId;
  group: SpendGroupId;
  key: string;
  label: string;
  costUsd: number;
};

export type SpendGroup = {
  id: SpendGroupId;
  label: string;
  hint: string;
  options: SpendOption[];
};

/** Providers always listed so Modal → Thunder migration stays visible. */
export const PRIMARY_COMPUTE_PROVIDERS = ["modal", "thunder"] as const;

export const COMPUTE_PROVIDER_LABELS: Record<string, string> = {
  modal: "Modal",
  daytona: "Daytona",
  archil: "Archil",
  numinous: "Numinous Cloud",
  thunder: "Thunder Compute",
  other: "Other",
  __other__: "Other",
};

const MODEL_PREFIX = "model:" as const;
const COMPUTE_PREFIX = "compute:" as const;

export function modelOptionId(key: string): SpendOptionId {
  return `${MODEL_PREFIX}${key}`;
}

export function computeOptionId(key: string): SpendOptionId {
  return `${COMPUTE_PREFIX}${key}`;
}

export function isSpendOptionId(value: string): value is SpendOptionId {
  return value.startsWith(MODEL_PREFIX) || value.startsWith(COMPUTE_PREFIX);
}

function seriesKeyTotal(series: CostSeries | undefined, key: string): number {
  if (!series) return 0;
  return series.buckets.reduce(
    (sum, bucket) => sum + (bucket.costs[key] ?? 0),
    0,
  );
}

function sortedModelOptions(series: CostSeries | undefined): SpendOption[] {
  if (!series) return [];
  return series.keys
    .map((entry) => ({
      id: modelOptionId(entry.key),
      group: "models" as const,
      key: entry.key,
      label: entry.label || entry.key,
      costUsd: seriesKeyTotal(series, entry.key),
    }))
    .sort((a, b) => b.costUsd - a.costUsd || a.label.localeCompare(b.label));
}

function computeOptions(series: CostSeries | undefined): SpendOption[] {
  const seen = new Set<string>();
  const options: SpendOption[] = [];

  const push = (key: string, label?: string) => {
    if (seen.has(key)) return;
    seen.add(key);
    options.push({
      id: computeOptionId(key),
      group: "compute",
      key,
      label: label ?? COMPUTE_PROVIDER_LABELS[key] ?? key,
      costUsd: seriesKeyTotal(series, key),
    });
  };

  for (const key of PRIMARY_COMPUTE_PROVIDERS) {
    push(key);
  }

  for (const entry of series?.keys ?? []) {
    push(entry.key, entry.label || COMPUTE_PROVIDER_LABELS[entry.key]);
  }

  // Prefer primary providers first, then remaining by spend.
  const primary = new Set<string>(PRIMARY_COMPUTE_PROVIDERS);
  const head = options.filter((o) => primary.has(o.key));
  const rest = options
    .filter((o) => !primary.has(o.key))
    .sort((a, b) => b.costUsd - a.costUsd || a.label.localeCompare(b.label));
  return [...head, ...rest];
}

export function buildSpendGroups(
  modelSeries: CostSeries | undefined,
  computeSeries: CostSeries | undefined,
): SpendGroup[] {
  return [
    {
      id: "compute",
      label: "Compute",
      hint: "Sandbox runtime estimates · Modal → Thunder transition",
      options: computeOptions(computeSeries),
    },
    {
      id: "models",
      label: "Models",
      hint: "Inference model spend for this window",
      options: sortedModelOptions(modelSeries),
    },
  ];
}

export function defaultSpendSelection(groups: SpendGroup[]): Set<SpendOptionId> {
  const withSpend = groups.flatMap((group) =>
    group.options.filter((option) => option.costUsd > 0).map((option) => option.id),
  );
  if (withSpend.length > 0) return new Set(withSpend);

  // Empty window: still select primary compute providers so the filter shows
  // the Modal/Thunder slots and empty-state copy stays useful.
  const fallback = groups.flatMap((group) =>
    group.id === "compute"
      ? group.options
          .filter((option) =>
            (PRIMARY_COMPUTE_PROVIDERS as readonly string[]).includes(option.key),
          )
          .map((option) => option.id)
      : [],
  );
  return new Set(fallback);
}

export function summarizeSpendSelection(
  groups: SpendGroup[],
  selected: ReadonlySet<SpendOptionId>,
): string {
  const selectedOptions = groups.flatMap((group) =>
    group.options.filter((option) => selected.has(option.id)),
  );
  if (selectedOptions.length === 0) return "Nothing selected";

  const allIds = groups.flatMap((group) => group.options.map((o) => o.id));
  if (allIds.length > 0 && allIds.every((id) => selected.has(id))) {
    return "All spend";
  }

  const compute = selectedOptions.filter((o) => o.group === "compute");
  const models = selectedOptions.filter((o) => o.group === "models");
  const parts: string[] = [];

  if (compute.length > 0) {
    const names = compute.map((o) => o.label);
    parts.push(
      names.length <= 2 ? names.join(" + ") : `Compute (${names.length})`,
    );
  }
  if (models.length > 0) {
    parts.push(
      models.length === 1 ? models[0].label : `${models.length} models`,
    );
  }
  return parts.join(" · ") || "Nothing selected";
}

export function toggleSpendOption(
  selected: ReadonlySet<SpendOptionId>,
  id: SpendOptionId,
): Set<SpendOptionId> {
  const next = new Set(selected);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
}

export function setGroupSelection(
  selected: ReadonlySet<SpendOptionId>,
  group: SpendGroup,
  checked: boolean,
): Set<SpendOptionId> {
  const next = new Set(selected);
  for (const option of group.options) {
    if (checked) next.add(option.id);
    else next.delete(option.id);
  }
  return next;
}

export function groupSelectionState(
  group: SpendGroup,
  selected: ReadonlySet<SpendOptionId>,
): boolean | "indeterminate" {
  if (group.options.length === 0) return false;
  const selectedCount = group.options.filter((o) => selected.has(o.id)).length;
  if (selectedCount === 0) return false;
  if (selectedCount === group.options.length) return true;
  return "indeterminate";
}

/** Merge selected model + compute series into one stacked chart series. */
export function mergeSpendSeries(
  modelSeries: CostSeries | undefined,
  computeSeries: CostSeries | undefined,
  selected: ReadonlySet<SpendOptionId>,
): CostSeries {
  const selectedModels = [...selected]
    .filter((id) => id.startsWith(MODEL_PREFIX))
    .map((id) => id.slice(MODEL_PREFIX.length));
  const selectedCompute = [...selected]
    .filter((id) => id.startsWith(COMPUTE_PREFIX))
    .map((id) => id.slice(COMPUTE_PREFIX.length));

  const modelLabel = new Map(
    (modelSeries?.keys ?? []).map((k) => [k.key, k.label || k.key]),
  );
  const computeLabel = new Map(
    (computeSeries?.keys ?? []).map((k) => [
      k.key,
      k.label || COMPUTE_PROVIDER_LABELS[k.key] || k.key,
    ]),
  );

  const keys = [
    ...selectedCompute.map((key) => ({
      key: computeOptionId(key),
      label: `${computeLabel.get(key) ?? COMPUTE_PROVIDER_LABELS[key] ?? key} · compute`,
    })),
    ...selectedModels.map((key) => ({
      key: modelOptionId(key),
      label: modelLabel.get(key) ?? key,
    })),
  ];

  const bucketStarts = new Set<string>();
  for (const bucket of modelSeries?.buckets ?? []) {
    bucketStarts.add(bucket.bucket_start);
  }
  for (const bucket of computeSeries?.buckets ?? []) {
    bucketStarts.add(bucket.bucket_start);
  }
  const sortedStarts = [...bucketStarts].sort();

  const modelByStart = new Map(
    (modelSeries?.buckets ?? []).map((b) => [b.bucket_start, b]),
  );
  const computeByStart = new Map(
    (computeSeries?.buckets ?? []).map((b) => [b.bucket_start, b]),
  );

  const buckets = sortedStarts.map((bucket_start) => {
    const costs: Record<string, number> = {};
    let costUsd = 0;
    let trialCount = 0;

    const modelBucket = modelByStart.get(bucket_start);
    if (modelBucket) {
      trialCount += modelBucket.trial_count;
      for (const key of selectedModels) {
        const value = modelBucket.costs[key] ?? 0;
        if (value > 0) {
          costs[modelOptionId(key)] = value;
          costUsd += value;
        }
      }
    }

    const computeBucket = computeByStart.get(bucket_start);
    if (computeBucket) {
      for (const key of selectedCompute) {
        const value = computeBucket.costs[key] ?? 0;
        if (value > 0) {
          costs[computeOptionId(key)] = value;
          costUsd += value;
        }
      }
    }

    return {
      bucket_start,
      cost_usd: Math.round(costUsd * 10000) / 10000,
      trial_count: trialCount,
      costs,
    };
  });

  return {
    dimension: "spend",
    keys,
    buckets,
  };
}

export function spendEmptyMessage(
  groups: SpendGroup[],
  selected: ReadonlySet<SpendOptionId>,
  series: CostSeries,
): string | null {
  const hasCost = series.buckets.some((b) => b.cost_usd > 0);
  if (hasCost) return null;
  if (selected.size === 0) {
    return "Select models and/or compute providers to plot.";
  }

  const selectedCompute = groups
    .find((g) => g.id === "compute")
    ?.options.filter((o) => selected.has(o.id)) ?? [];
  const selectedModels =
    groups.find((g) => g.id === "models")?.options.filter((o) => selected.has(o.id)) ??
    [];

  const onlyModal =
    selectedCompute.length === 1 &&
    selectedCompute[0]?.key === "modal" &&
    selectedModels.length === 0;
  const onlyThunder =
    selectedCompute.length === 1 &&
    selectedCompute[0]?.key === "thunder" &&
    selectedModels.length === 0;

  if (onlyModal) {
    return "No Modal compute spend in this window. Thunder may hold newer sandbox runtime cost.";
  }
  if (onlyThunder) {
    return "No Thunder Compute spend in this window yet.";
  }
  if (selectedModels.length > 0 && selectedCompute.length === 0) {
    return "No model spend for the selected models in this window.";
  }
  if (selectedCompute.length > 0 && selectedModels.length === 0) {
    return "No compute spend for the selected providers in this window.";
  }
  return "No spend for the current selection in this window.";
}

export type ComputeTransitionState = {
  modalCost: number;
  thunderCost: number;
  label: string | null;
};

export function computeTransitionState(
  computeSeries: CostSeries | undefined,
): ComputeTransitionState {
  const modalCost = seriesKeyTotal(computeSeries, "modal");
  const thunderCost = seriesKeyTotal(computeSeries, "thunder");
  let label: string | null = null;
  if (thunderCost > 0 && modalCost > 0) {
    label = "Modal and Thunder both reporting compute";
  } else if (thunderCost > 0 && modalCost <= 0) {
    label = "Compute on Thunder (Modal idle in this window)";
  } else if (modalCost > 0 && thunderCost <= 0) {
    label = "Compute still on Modal · Thunder not yet in this window";
  } else {
    label = "No Modal or Thunder compute in this window";
  }
  return { modalCost, thunderCost, label };
}
