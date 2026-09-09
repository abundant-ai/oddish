import { Loader2 } from "lucide-react";

// Footer status for the trial pages that are still streaming in. It replaces
// the old "load more" button: pages request themselves, so the user only
// needs to know that more rows are still on the way.
export function ExperimentTrialLoadProgress({
  loaded,
  total,
}: {
  loaded: number;
  total: number;
}) {
  return (
    <div
      className="text-muted-foreground flex items-center justify-center gap-2 py-3 text-xs"
      role="status"
      aria-live="polite"
    >
      <Loader2 className="h-3.5 w-3.5 animate-spin" />
      <span>
        Loading trial results
        {total > 0
          ? ` ${loaded.toLocaleString()}/${total.toLocaleString()}`
          : ""}
        …
      </span>
    </div>
  );
}
