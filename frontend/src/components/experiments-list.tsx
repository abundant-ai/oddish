import Link from "next/link";

import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { cn, encodeExperimentRouteParam } from "@/lib/utils";

export type ExperimentRef = { id: string; name: string };

/**
 * Renders the experiments a task belongs to. Shows the first `maxVisible`
 * and collapses the rest behind a "+N more" popover that lists every
 * experiment in a scrollable panel.
 *
 * `layout`:
 *  - "inline"  — ·-separated on one wrapping line (task-detail header)
 *  - "stacked" — one experiment per line (task card, where the narrow box
 *    makes ·-separated names wrap awkwardly)
 *
 * Shared between the header and the /tasks card so both render affiliated
 * experiments consistently.
 */
export function ExperimentsList({
  experiments,
  maxVisible = 5,
  layout = "inline",
  className,
  linkClassName,
}: {
  experiments: ExperimentRef[];
  maxVisible?: number;
  layout?: "inline" | "stacked";
  className?: string;
  /** Applied to the experiment links so each consumer can match its
   * surrounding text color. */
  linkClassName?: string;
}) {
  if (experiments.length === 0) return null;

  const visible = experiments.slice(0, maxVisible);
  const hidden = experiments.slice(maxVisible);
  const overflowCount = hidden.length;

  const moreButton =
    overflowCount > 0 ? (
      <Popover>
        <PopoverTrigger asChild>
          <button
            type="button"
            className="text-muted-foreground hover:text-foreground rounded text-left underline-offset-2 hover:underline"
          >
            +{overflowCount} more
          </button>
        </PopoverTrigger>
        <PopoverContent
          align="start"
          className="max-h-72 w-64 overflow-y-auto p-1"
        >
          <div className="flex flex-col">
            {hidden.map((exp) => (
              <Link
                key={exp.id}
                href={`/experiments/${encodeExperimentRouteParam(exp.id)}`}
                className="hover:bg-muted truncate rounded px-2 py-1 text-xs"
                title={exp.name}
              >
                {exp.name}
              </Link>
            ))}
          </div>
        </PopoverContent>
      </Popover>
    ) : null;

  if (layout === "stacked") {
    return (
      <div className={cn("flex flex-col gap-0.5", className)}>
        {visible.map((exp) => (
          <Link
            key={exp.id}
            href={`/experiments/${encodeExperimentRouteParam(exp.id)}`}
            className={cn(
              "truncate underline-offset-2 hover:underline",
              linkClassName
            )}
            title={exp.name}
          >
            {exp.name}
          </Link>
        ))}
        {moreButton}
      </div>
    );
  }

  return (
    <span
      className={cn(
        "inline-flex flex-wrap items-center gap-x-2 gap-y-1",
        className
      )}
    >
      {visible.map((exp, i) => (
        <span key={exp.id} className="inline-flex items-center gap-x-2">
          {i > 0 ? <span aria-hidden>·</span> : null}
          <Link
            href={`/experiments/${encodeExperimentRouteParam(exp.id)}`}
            className={cn("underline-offset-2 hover:underline", linkClassName)}
          >
            {exp.name}
          </Link>
        </span>
      ))}
      {moreButton ? (
        <span className="inline-flex items-center gap-x-2">
          <span aria-hidden>·</span>
          {moreButton}
        </span>
      ) : null}
    </span>
  );
}
