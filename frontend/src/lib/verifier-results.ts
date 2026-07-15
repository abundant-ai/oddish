export interface CtrfSummary {
  tests: number;
  passed: number;
  failed: number;
  skipped: number;
  pending: number;
  other: number;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function nonnegativeInteger(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 0
    ? value
    : null;
}

function parseCtrfSummary(value: unknown): CtrfSummary | null {
  if (!isRecord(value) || value.format !== "ctrf") return null;

  const tests = nonnegativeInteger(value.tests);
  const passed = nonnegativeInteger(value.passed);
  const failed = nonnegativeInteger(value.failed);
  const skipped = nonnegativeInteger(value.skipped);
  const pending = nonnegativeInteger(value.pending);
  const other = nonnegativeInteger(value.other);
  if (
    tests === null ||
    passed === null ||
    failed === null ||
    skipped === null ||
    pending === null ||
    other === null
  ) {
    return null;
  }

  return { tests, passed, failed, skipped, pending, other };
}

export function embeddedCtrfSummary(
  result: Record<string, unknown> | null | undefined,
): CtrfSummary | null {
  return parseCtrfSummary(result?._verifier);
}

export function parseCtrfReport(value: unknown): CtrfSummary | null {
  if (!isRecord(value) || !isRecord(value.results)) return null;
  if (!isRecord(value.results.summary)) return null;
  return parseCtrfSummary({ format: "ctrf", ...value.results.summary });
}
