export interface CtrfSummary {
  format: "ctrf";
  tests: number;
  passed: number;
  failed: number;
  skipped: number;
  pending: number;
  other: number;
  tool?: string;
}

export interface VerifierMetric {
  key: string;
  label: string;
  value: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function nonnegativeInteger(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 0
    ? value
    : null;
}

export function parseCtrfSummary(value: unknown): CtrfSummary | null {
  if (!isRecord(value)) return null;

  const tests = nonnegativeInteger(value.tests);
  const passed = nonnegativeInteger(value.passed);
  const failed = nonnegativeInteger(value.failed);
  const skipped = nonnegativeInteger(value.skipped);
  const pending = nonnegativeInteger(value.pending);
  const other = nonnegativeInteger(value.other);
  if (
    value.format !== "ctrf" ||
    tests === null ||
    passed === null ||
    failed === null ||
    skipped === null ||
    pending === null ||
    other === null
  ) {
    return null;
  }

  return {
    format: "ctrf",
    tests,
    passed,
    failed,
    skipped,
    pending,
    other,
    ...(typeof value.tool === "string" && value.tool.trim()
      ? { tool: value.tool.trim() }
      : {}),
  };
}

export function embeddedCtrfSummary(
  result: Record<string, unknown> | null | undefined
): CtrfSummary | null {
  return parseCtrfSummary(result?._verifier);
}

export function parseCtrfReport(value: unknown): CtrfSummary | null {
  if (!isRecord(value) || !isRecord(value.results)) return null;
  const results = value.results;
  if (!isRecord(results.summary)) return null;

  const tool =
    isRecord(results.tool) && typeof results.tool.name === "string"
      ? results.tool.name
      : undefined;
  return parseCtrfSummary({
    format: "ctrf",
    ...results.summary,
    ...(tool ? { tool } : {}),
  });
}

const RESERVED_RESULT_KEYS = new Set(["schema_version", "harbor_exception"]);

function metricNumber(value: number): string {
  return new Intl.NumberFormat("en-US", {
    maximumFractionDigits: Math.abs(value) >= 100 ? 0 : 2,
  }).format(value);
}

function metricLabel(key: string): string {
  return key
    .replace(/_(ms|pct|percent|per_sec|seconds|sec|bytes)$/i, "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function metricValue(key: string, value: string | number | boolean): string {
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "string") return value;

  const formatted = metricNumber(value);
  if (/_ms$/i.test(key)) return `${formatted} ms`;
  if (/_(pct|percent)$/i.test(key)) return `${formatted}%`;
  if (/_per_sec$/i.test(key)) return `${formatted}/s`;
  if (/_(seconds|sec)$/i.test(key)) return `${formatted} s`;
  return formatted;
}

export function verifierMetrics(
  result: Record<string, unknown> | null | undefined,
  limit = 6
): VerifierMetric[] {
  if (!result) return [];

  const metrics: VerifierMetric[] = [];
  for (const [key, value] of Object.entries(result)) {
    if (
      key.startsWith("_") ||
      RESERVED_RESULT_KEYS.has(key) ||
      !["string", "number", "boolean"].includes(typeof value) ||
      (typeof value === "number" && !Number.isFinite(value))
    ) {
      continue;
    }
    metrics.push({
      key,
      label: metricLabel(key),
      value: metricValue(key, value as string | number | boolean),
    });
    if (metrics.length >= limit) break;
  }
  return metrics;
}
