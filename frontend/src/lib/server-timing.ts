type ServerTimingMetric = {
  name: string;
  durationMs: number;
  description?: string;
};

function sanitizeMetricName(name: string): string {
  const sanitized = name.trim().replace(/[^a-zA-Z0-9_-]/g, "_");
  return sanitized.length > 0 ? sanitized : "metric";
}

function quoteDescription(description: string): string {
  return description.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
}

function formatServerTiming(metrics: ServerTimingMetric[]): string {
  return metrics
    .filter((metric) => Number.isFinite(metric.durationMs))
    .map((metric) => {
      const parts = [
        `${sanitizeMetricName(metric.name)};dur=${Math.max(metric.durationMs, 0).toFixed(1)}`,
      ];
      if (metric.description) {
        parts.push(`desc="${quoteDescription(metric.description)}"`);
      }
      return parts.join(";");
    })
    .join(", ");
}

export function joinServerTimingHeaders(
  ...headers: Array<string | null | undefined>
): string | null {
  const joined = headers
    .map((header) => header?.trim())
    .filter((header): header is string => Boolean(header))
    .join(", ");
  return joined.length > 0 ? joined : null;
}

export class ServerTimingCollector {
  private readonly metrics: ServerTimingMetric[] = [];

  add(name: string, durationMs: number, description?: string): void {
    this.metrics.push({ name, durationMs, description });
  }

  measure<T>(name: string, fn: () => T, description?: string): T {
    const startedAt = performance.now();
    const result = fn();
    this.add(name, performance.now() - startedAt, description);
    return result;
  }

  async measureAsync<T>(
    name: string,
    fn: () => Promise<T>,
    description?: string,
  ): Promise<T> {
    const startedAt = performance.now();
    try {
      return await fn();
    } finally {
      this.add(name, performance.now() - startedAt, description);
    }
  }

  toHeader(): string | null {
    return formatServerTiming(this.metrics);
  }
}
