/**
 * Unbiased pass@k estimator
 *
 * Formula: pass@k = 1 - C(n-c, k) / C(n, k)
 *
 * Where:
 * - n = total number of attempts
 * - c = number of correct/passing attempts
 * - k = the k value (how many attempts allowed)
 *
 * This is computed using the product form to avoid large factorials:
 * pass@k = 1 - ∏(i=0 to k-1) [(n-c-i)/(n-i)]
 */
export function calculatePassAtK(n: number, c: number, k: number): number {
  // Edge cases
  if (k > n) return c > 0 ? 1 : 0;
  if (c === 0) return 0;
  if (c >= n) return 1;
  if (k <= 0) return 0;

  // If k > n - c, we're guaranteed at least one correct
  if (k > n - c) return 1;

  // Calculate using product form: 1 - ∏(i=0 to k-1) [(n-c-i)/(n-i)]
  let product = 1;
  for (let i = 0; i < k; i++) {
    product *= (n - c - i) / (n - i);
  }

  return 1 - product;
}

interface PassAtKDataPoint {
  k: number;
  [agent: string]: number; // pass@k value for each agent
}

export interface AgentPassAtKStats {
  // Upper bound for the chart's k axis; each task contributes its own
  // n to the formula via ``taskResults[i].n`` and falls back to this
  // value when omitted.
  n: number;
  taskResults: { task: string; c: number; n?: number }[];
}

/**
 * Calculate pass@k curve data for multiple agents
 * Returns data points for k = 1 to maxK
 */
export function calculatePassAtKCurve(
  agentStats: Record<string, AgentPassAtKStats>,
  maxK?: number,
): PassAtKDataPoint[] {
  const agents = Object.keys(agentStats);
  if (agents.length === 0) return [];

  // Find the maximum n across all agents to determine maxK
  const maxN = Math.max(...Object.values(agentStats).map((s) => s.n));
  const effectiveMaxK = maxK ?? maxN;

  if (effectiveMaxK <= 0) return [];

  const dataPoints: PassAtKDataPoint[] = [];

  for (let k = 1; k <= effectiveMaxK; k++) {
    const point: PassAtKDataPoint = { k };

    for (const agent of agents) {
      const stats = agentStats[agent];
      if (stats.taskResults.length === 0) {
        point[agent] = 0;
        continue;
      }

      // Calculate average pass@k across all tasks for this agent.
      // Each task's n may differ (different attempts per cell) -- fall
      // back to the agent's overall n only when the per-task n isn't
      // given.
      const passAtKValues = stats.taskResults.map(({ c, n }) =>
        calculatePassAtK(n ?? stats.n, c, k),
      );

      // Average across tasks
      point[agent] =
        passAtKValues.reduce((a, b) => a + b, 0) / passAtKValues.length;
    }

    dataPoints.push(point);
  }

  return dataPoints;
}
