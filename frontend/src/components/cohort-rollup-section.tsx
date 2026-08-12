"use client";

import type { ReactNode } from "react";
import useSWR from "swr";

import {
  CHART_CATEGORIES,
  RADAR_MODEL_CAP,
  radarModels,
} from "@/lib/cohort-metrics";
import type {
  CohortRollup,
  CohortRollupCategory,
  CohortRollupModelCell,
} from "@/lib/types";
import { cn, encodeExperimentRouteParam } from "@/lib/utils";

const CATEGORY_LABELS: Record<string, string> = {
  planning: "Planning",
  testing_verification: "Testing and verification",
  debugging: "Debugging",
  scope_adherence: "Scope adherence",
  coherence: "Long-horizon coherence",
  environment_tooling: "Environment and tooling",
};

/** Short forms for the axis ends, where the full names would collide with each
 *  other and with the shapes. The table below the chart carries the full name,
 *  so nothing is only ever named by its abbreviation. */
const AXIS_LABELS: Record<string, string> = {
  planning: "Planning",
  testing_verification: "Testing",
  debugging: "Debugging",
  scope_adherence: "Scope",
  coherence: "Coherence",
  environment_tooling: "Environment",
};

/** Documented categorical slots 1-3 (blue, orange, aqua), light step then dark
 *  step. Validated `--pairs all` -- three translucent shapes overlap, so every
 *  pair can end up adjacent -- against `--card` in both modes: worst CVD dE 9.2
 *  light / 9.4 dark, normal-vision 24.0 / 20.9. Light mode WARNs on contrast
 *  (orange 2.97, aqua 2.61 against `#f9f6f1`), which obliges a relief channel:
 *  the legend names every plotted model and the table below repeats every cell
 *  as a number, so colour never carries a value on its own. Re-validate both
 *  modes against `--card` after any change. */
const MODEL_INK = [
  "text-[#2a78d6] dark:text-[#3987e5]",
  "text-[#eb6834] dark:text-[#d95926]",
  "text-[#1baf7a] dark:text-[#199e70]",
];

/** Radial band the shapes live in. The baseline ring sits at the midpoint of
 *  the band, so a model beating its own average bulges outward and one trailing
 *  it pulls inward by the same distance per unit of delta. The inner radius is
 *  not 0: three shapes converging on a single centre point stop being three
 *  shapes, and a maximally-below-baseline vertex still has to be visible. */
const R_MAX = 100;
const R_MIN = 20;
const R_BASE = (R_MAX + R_MIN) / 2;

/** Smallest half-range the radial scale will use. Below it, rounding noise on a
 *  handful of citations would fill the whole chart. */
const MIN_DOMAIN = 0.1;

function axisAngle(i: number): number {
  // First axis points straight up; SVG y grows downward, so the sweep runs
  // clockwise from -90 degrees.
  return -Math.PI / 2 + (i * 2 * Math.PI) / CHART_CATEGORIES.length;
}

function polar(i: number, radius: number): [number, number] {
  const a = axisAngle(i);
  return [radius * Math.cos(a), radius * Math.sin(a)];
}

/** delta -> radius. `domain` is the delta that reaches the outer ring; it is
 *  computed from the data and printed under the chart, because a radar with an
 *  unstated scale invites reading the shape as an absolute score. */
function radiusFor(delta: number, domain: number): number {
  const t = Math.max(-1, Math.min(1, delta / domain));
  return R_BASE + t * (R_MAX - R_BASE);
}

function points(pts: ([number, number] | null)[]): string {
  return pts
    .filter((p): p is [number, number] => p !== null)
    .map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`)
    .join(" ");
}

/** Maximal runs of consecutive present axes, walking the ring. An absent axis
 *  is a break in the outline, never a vertex: joining across it would draw a
 *  value the evidence does not support. Returns one wrapping run when every
 *  axis is present, which the caller draws as a closed polygon. */
function presentRuns(present: boolean[]): number[][] {
  const count = present.length;
  if (present.every(Boolean)) return [present.map((_, i) => i)];
  const gap = present.indexOf(false);
  const runs: number[][] = [];
  let run: number[] = [];
  for (let k = 1; k <= count; k++) {
    const i = (gap + k) % count;
    if (present[i]) {
      run.push(i);
    } else if (run.length) {
      runs.push(run);
      run = [];
    }
  }
  if (run.length) runs.push(run);
  return runs;
}

function signed(value: number): string {
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}`;
}

/** True when over half of a model's drawn vertices rest on fewer than
 *  `thinThreshold` cited runs — the shape is a sketch, not a measurement. */
function isProvisional(
  cells: (CohortRollupModelCell | undefined)[],
  present: boolean[],
  thinThreshold: number,
): boolean {
  const drawn = present.filter(Boolean).length;
  const thin = present.filter(
    (p, i) => p && (cells[i]?.n ?? 0) < thinThreshold,
  ).length;
  return thin * 2 > drawn;
}

function ShapeForModel({
  model,
  ink,
  axes,
  domain,
  thinThreshold,
}: {
  model: string;
  ink: string;
  axes: (CohortRollupCategory | undefined)[];
  domain: number;
  thinThreshold: number;
}) {
  const cells = axes.map((cat) => cat?.per_model.find((c) => c.model === model));
  const pts = cells.map((cell, i) => {
    // n === 0 is an absent vertex, not a vertex at the baseline.
    if (!cell || cell.n === 0 || cell.delta === null) return null;
    return polar(i, radiusFor(cell.delta, domain));
  });
  const present = pts.map((p) => p !== null);
  if (!present.some(Boolean)) return null;

  // A fill asserts an enclosed region, and a shape missing a vertex encloses
  // nothing: any polygon over the surviving points spans the absent axis with
  // an edge a reader would read as a value. So a gapped shape is an open
  // outline -- visibly incomplete -- and only a complete one is filled.
  const closed = present.every(Boolean);

  // Weight of evidence has to be visible in the mark itself. A shape built
  // mostly from thin cells keeps its geometry but loses its fill and goes to a
  // thinner dashed stroke, so it cannot be mistaken at a glance for one backed
  // by full cohorts; per-vertex, a thin cell is drawn hollow.
  const provisional = isProvisional(cells, present, thinThreshold);
  const strokeProps = provisional
    ? {
        strokeWidth: 1.25,
        strokeOpacity: 0.75,
        strokeDasharray: "4 3",
        strokeLinejoin: "round" as const,
      }
    : { strokeWidth: 1.75, strokeLinejoin: "round" as const };

  return (
    <g className={ink} fill="none" stroke="currentColor">
      {closed ? (
        <>
          {!provisional && (
            <polygon
              points={points(pts)}
              fill="currentColor"
              fillOpacity={0.14}
              stroke="none"
            />
          )}
          <polygon points={points(pts)} {...strokeProps} />
        </>
      ) : (
        presentRuns(present)
          .filter((run) => run.length >= 2)
          .map((run, i) => (
            <polyline
              key={i}
              points={points(run.map((idx) => pts[idx]))}
              {...strokeProps}
              strokeLinecap="round"
            />
          ))
      )}
      {pts.map((p, i) =>
        p === null ? null : (cells[i]?.n ?? 0) < thinThreshold ? (
          <circle key={i} cx={p[0]} cy={p[1]} r={2.75} strokeWidth={1.25} />
        ) : (
          <circle key={i} cx={p[0]} cy={p[1]} r={2.75} fill="currentColor" stroke="none" />
        ),
      )}
    </g>
  );
}

function Radar({
  rollup,
  series,
  axes,
  domain,
}: {
  rollup: CohortRollup;
  series: { model: string; ink: string }[];
  axes: (CohortRollupCategory | undefined)[];
  domain: number;
}) {
  return (
    <svg
      viewBox="-178 -136 356 272"
      className="h-auto w-full max-w-[440px]"
      role="img"
      aria-label={
        `Cited-success share against each model's own success rate, over ` +
        `${CHART_CATEGORIES.length} behaviour categories. Outer ring ` +
        `${signed(domain)}, middle ring 0, inner ring ${signed(-domain)}. ` +
        `Hollow points and dashed outlines mark evidence under ` +
        `${rollup.thin_threshold} cited runs. ` +
        `The same values are tabulated below.`
      }
    >
      <g className="text-border" stroke="currentColor" fill="none">
        <circle r={R_MAX} strokeDasharray="3 3" />
        <circle r={R_MIN} strokeDasharray="3 3" />
        {axes.map((cat, i) => {
          const [x, y] = polar(i, R_MAX);
          // A category nothing cited gets a dashed spoke: the axis exists, the
          // evidence does not.
          return (
            <line
              key={i}
              x1={0}
              y1={0}
              x2={x}
              y2={y}
              strokeDasharray={cat && cat.pooled.n > 0 ? undefined : "2 4"}
            />
          );
        })}
      </g>
      {/* Every model's own success rate, all on one ring: the radius plots each
          model's delta against its own baseline, which is what makes one ring
          right for all of them. */}
      <circle
        r={R_BASE}
        className="text-muted-foreground"
        stroke="currentColor"
        fill="none"
        strokeWidth={1}
      />
      {series.map((s) => (
        <ShapeForModel
          key={s.model}
          model={s.model}
          ink={s.ink}
          axes={axes}
          domain={domain}
          thinThreshold={rollup.thin_threshold}
        />
      ))}
      <g className="text-muted-foreground" fill="currentColor" fontSize={11}>
        {CHART_CATEGORIES.map((category, i) => {
          const [x, y] = polar(i, R_MAX + 14);
          const cos = Math.cos(axisAngle(i));
          const sin = Math.sin(axisAngle(i));
          // Six axes put a label at sin = -1, ±0.5 or 1 and cos = 0 or ±0.866,
          // so the thresholds only have to separate "straight up/down" from
          // "off to one side" -- they are nowhere near a boundary.
          const anchor =
            Math.abs(cos) < 0.1 ? "middle" : cos > 0 ? "start" : "end";
          const cat = axes[i];
          return (
            <text
              key={category}
              x={x}
              y={y}
              textAnchor={anchor}
              dy={sin > 0.9 ? "0.8em" : sin < -0.9 ? "-0.2em" : "0.35em"}
              opacity={cat && cat.pooled.n > 0 ? 1 : 0.45}
            >
              {AXIS_LABELS[category] ?? category}
            </text>
          );
        })}
      </g>
      <title>
        Behaviour deltas for {series.map((s) => s.model).join(", ")} across{" "}
        {rollup.coverage.task_versions_compared} compared task versions.
      </title>
    </svg>
  );
}

/** How many uncompared tasks to name before switching to a count. A sweep can
 *  carry hundreds, and an unbounded list buries the coverage claim above it. */
const MISSING_NAMED = 8;

function CoverageLine({ rollup }: { rollup: CohortRollup }) {
  const { task_versions_compared, task_versions_total, missing } =
    rollup.coverage;
  // Only a version the generator would actually accept is offered as a link.
  // One under the cohort gate cannot be compared as it stands, so linking it
  // invites the reader to go and ask for something that will be refused.
  const runnable = missing.filter((m) => m.reason !== "below_cohort_gate");
  const gated = missing.length - runnable.length;
  const named = runnable.slice(0, MISSING_NAMED);
  const unnamed = runnable.length - named.length;

  return (
    <div className="flex flex-col gap-1">
      <p className="text-muted-foreground text-xs">
        Based on {task_versions_compared} of {task_versions_total} task
        {task_versions_total === 1 ? "" : "s"}.
      </p>
      {named.length > 0 && (
        <p className="text-muted-foreground text-xs">
          Not compared yet:{" "}
          {named.map((m, i) => (
            <span key={`${m.task_id}-${m.version}`}>
              {i > 0 && ", "}
              <a
                href={`/tasks/${encodeURIComponent(m.task_id)}`}
                className="underline-offset-4 hover:underline"
              >
                {m.task_name} v{m.version}
              </a>
            </span>
          ))}
          {unnamed > 0 && ` +${unnamed} more`}.
        </p>
      )}
      {gated > 0 && (
        <p className="text-muted-foreground text-xs">
          {gated} task{gated === 1 ? "" : "s"} {gated === 1 ? "has" : "have"} too
          few classified runs on either side to compare.
        </p>
      )}
    </div>
  );
}

export function CohortRollupSection({
  experimentId,
  apiBaseUrl = "/api",
}: {
  experimentId: string;
  apiBaseUrl?: string;
}) {
  const { data, error, isLoading } = useSWR<CohortRollup>(
    `${apiBaseUrl}/experiments/${encodeExperimentRouteParam(experimentId)}/cohort-rollup`,
    (url: string) =>
      fetch(url).then((r) => (r.ok ? r.json() : Promise.reject(r.status))),
    { shouldRetryOnError: false },
  );

  if (isLoading) {
    return (
      <Frame>
        <p className="text-muted-foreground animate-pulse text-xs">
          Gathering behaviour comparisons across this experiment&rsquo;s tasks…
        </p>
      </Frame>
    );
  }

  if (error) {
    return (
      <Frame>
        <p className="text-muted-foreground text-xs">
          Could not build the rollup
          {typeof error === "number" ? ` (${error})` : ""}. Reload to try again.
        </p>
      </Frame>
    );
  }

  // Nothing to be covered: no task version in this experiment can carry a
  // comparison, so there is no coverage claim to make either.
  if (!data || data.coverage.task_versions_total === 0) return null;

  // No comparison is stored yet. Say so, and name what is uncovered -- an
  // empty chart frame would imply the shapes are still loading.
  if (data.coverage.task_versions_compared === 0) {
    return (
      <Frame>
        <CoverageLine rollup={data} />
      </Frame>
    );
  }

  const byCategory = new Map(data.categories.map((c) => [c.category, c]));
  // Driven off CHART_CATEGORIES, not the response's order: six axes, in one
  // fixed order, whatever the payload happens to list.
  const axes = CHART_CATEGORIES.map((c) => byCategory.get(c));

  const drawn = radarModels(data);
  // Hue slots are handed out in model-name order, which nothing in the data can
  // move. The payload's own order is NOT a stable key: `_model_rows` sorts it by
  // cohort size, so one more trial can reorder it and repaint shapes whose
  // evidence did not change. What a name sort cannot fix is a membership change
  // -- with three slots and more than three models, a model dropping out still
  // shifts the survivors below it -- so it buys stability under new data, not
  // under a different cast.
  const series = [...drawn]
    .sort((a, b) => (a.model < b.model ? -1 : a.model > b.model ? 1 : 0))
    .map((m, slot) => ({ ...m, ink: MODEL_INK[slot] }));

  // Distinct trials in the model's cohort. The legend shows this, not the
  // `radarModels` total: that total sums one distinct-trial count per category,
  // so a trial cited in four categories counts four times and reads as far more
  // evidence than exists.
  const runsOf = new Map(
    data.models.map((m) => [m.model, m.cohort_success + m.cohort_failure]),
  );

  const domain = Math.max(
    MIN_DOMAIN,
    Math.ceil(
      series.reduce((worst, s) => {
        for (const cat of axes) {
          const cell = cat?.per_model.find((c) => c.model === s.model);
          if (cell?.delta != null && cell.n > 0)
            worst = Math.max(worst, Math.abs(cell.delta));
        }
        return worst;
      }, 0) *
        20 -
        1e-9,
    ) / 20,
  );

  const belowBar = data.models.length - drawn.length;
  const emptyAxes = CHART_CATEGORIES.filter(
    (c) => (byCategory.get(c)?.pooled.n ?? 0) === 0,
  );
  // Whether any drawn vertex is thin, which is what the hollow-point and
  // dashed-outline treatments key on. The note is only worth the line when the
  // reader can actually see one.
  const anyThin = series.some((s) =>
    axes.some((cat) => {
      const cell = cat?.per_model.find((c) => c.model === s.model);
      return cell != null && cell.n > 0 && cell.n < data.thin_threshold;
    }),
  );

  return (
    <Frame>
      <CoverageLine rollup={data} />
      {series.length === 0 ? (
        <p className="text-muted-foreground text-xs">
          No model has {data.thin_threshold} distinct runs cited across the six
          categories, so no shape is drawn — the cells are in the table below.
        </p>
      ) : (
        <div className="flex flex-col items-center gap-2">
          <Radar rollup={data} series={series} axes={axes} domain={domain} />
          <ul className="flex flex-wrap justify-center gap-x-4 gap-y-1">
            {series.map((s) => (
              <li key={s.model} className="flex items-center gap-1.5 text-xs">
                <span
                  className={cn(s.ink, "bg-current h-2.5 w-2.5 rounded-[2px]")}
                  aria-hidden="true"
                />
                <span>{s.model}</span>
                <span className="text-muted-foreground font-mono">
                  {runsOf.get(s.model) ?? 0} runs
                </span>
              </li>
            ))}
          </ul>
          <p className="text-muted-foreground max-w-prose text-center text-xs">
            Distance from the middle ring is a model&rsquo;s cited-success share
            on that category minus its own success rate across these tasks —
            outside the ring it beats its own average, inside it trails. Outer
            ring {signed(domain)}, inner ring {signed(-domain)}.
          </p>
          {anyThin && (
            <p className="text-muted-foreground max-w-prose text-center text-xs">
              A hollow point rests on fewer than {data.thin_threshold} cited
              runs; a shape mostly made of those is drawn as a dashed outline
              with no fill.
            </p>
          )}
        </div>
      )}
      {emptyAxes.length > 0 && (
        <p className="text-muted-foreground text-xs">
          No run was cited for{" "}
          {emptyAxes.map((c) => CATEGORY_LABELS[c] ?? c).join(", ")}; those axes
          are blank rather than zero.
        </p>
      )}
      {series.length > 0 && belowBar > 0 && (
        <p className="text-muted-foreground text-xs">
          Charting the {series.length} model
          {series.length === 1 ? "" : "s"} with the most citations across the six
          categories (at most {RADAR_MODEL_CAP}). {belowBar} other
          {belowBar === 1 ? " is" : "s are"} in the table only — either past that
          cap or backed by under {data.thin_threshold} distinct cited runs, which
          is too few to draw a shape from.
        </p>
      )}
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          {/* This table is the relief channel that makes the chart's palette
              acceptable -- two of the three hues only reach WARN contrast on
              the light card, so colour never carries a value on its own. That
              only holds if a screen reader can attach a cell to its model, so
              the model name is a row header, not a plain cell. */}
          <caption className="sr-only">
            Behaviour delta and cited-run count for every model and category,
            the same values the chart plots.
          </caption>
          <thead>
            <tr className="text-muted-foreground text-left">
              <th scope="col" className="py-1 pr-3 font-medium">
                Model
              </th>
              {CHART_CATEGORIES.map((c) => (
                <th key={c} scope="col" className="py-1 pr-3 font-medium">
                  {CATEGORY_LABELS[c] ?? c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.models.map((m) => {
              const ink = series.find((s) => s.model === m.model)?.ink;
              return (
                <tr key={m.model} className="border-border border-t align-top">
                  <th scope="row" className="py-1 pr-3 text-left font-normal">
                    <span className="flex items-center gap-1.5">
                      {ink && (
                        <span
                          className={cn(ink, "bg-current h-2 w-2 shrink-0 rounded-[2px]")}
                          aria-hidden="true"
                        />
                      )}
                      <span>{m.model}</span>
                    </span>
                    <span className="text-muted-foreground font-mono">
                      base {m.baseline.toFixed(2)}
                    </span>
                  </th>
                  {CHART_CATEGORIES.map((c) => {
                    const cell = byCategory
                      .get(c)
                      ?.per_model.find((p) => p.model === m.model);
                    return (
                      <td key={c} className="py-1 pr-3 font-mono">
                        {!cell || cell.n === 0 || cell.delta === null ? (
                          <span className="text-muted-foreground">—</span>
                        ) : (
                          <>
                            {signed(cell.delta)}
                            <span className="text-muted-foreground">
                              {" "}
                              n={cell.n}
                            </span>
                          </>
                        )}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Frame>
  );
}

function Frame({ children }: { children: ReactNode }) {
  return (
    <section className="border-border bg-card flex flex-col gap-3 rounded-lg border p-4">
      <h3 className="text-sm font-semibold">Behaviour across this experiment</h3>
      {children}
    </section>
  );
}
