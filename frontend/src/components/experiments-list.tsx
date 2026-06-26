import Link from "next/link";

import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { cn, encodeExperimentRouteParam } from "@/lib/utils";

export type ExperimentRef = { id: string; name: string };

/**
 * Renders a list of experiments a task belongs to. Shows the first
 * `maxVisible` inline (·-separated) and collapses the rest behind a
 * "+N more" popover that lists every experiment in a scrollable panel.
 *
 * Shared between the task-detail header and the /tasks card so both views
 * render affiliated experiments identically.
 */
export function ExperimentsList({
  experiments,
  maxVisible = 5,
  className,
  linkClassName,
}: {
  experiments: ExperimentRef[];
  maxVisible?: number;
  className?: string;
  /** Applied to the inline experiment links so each consumer can match its
   * surrounding text color. */
  linkClassName?: string;
}) {
  if (experiments.length === 0) return null;

  const visible = experiments.slice(0, maxVisible);
  const overflowCount = experiments.length - visible.length;

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
      {overflowCount > 0 ? (
        <span className="inline-flex items-center gap-x-2">
          <span aria-hidden>·</span>
          <Popover>
            <PopoverTrigger asChild>
              <button
                type="button"
                className="text-muted-foreground hover:text-foreground rounded underline-offset-2 hover:underline"
              >
                +{overflowCount} more
              </button>
            </PopoverTrigger>
            <PopoverContent
              align="start"
              className="max-h-72 w-64 overflow-y-auto p-1"
            >
              <div className="flex flex-col">
                {experiments.map((exp) => (
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
        </span>
      ) : null}
    </span>
  );
}
