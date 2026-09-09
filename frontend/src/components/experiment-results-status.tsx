import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import type { ExperimentPageSummary } from "@/lib/types";

export function ExperimentResultsStatus({
  summary,
  tasksLoaded,
  trialsLoaded,
  complete,
  isLoading,
  hasError,
  onRetry,
}: {
  summary?: ExperimentPageSummary | null;
  tasksLoaded: number;
  trialsLoaded: number;
  complete: boolean;
  isLoading: boolean;
  hasError: boolean;
  onRetry: () => void;
}) {
  const counts = summary ? (
    <span>
      {tasksLoaded.toLocaleString()} of {summary.task_count.toLocaleString()}{" "}
      tasks loaded · {trialsLoaded.toLocaleString()} of{" "}
      {summary.trial_count.toLocaleString()} trial results loaded
    </span>
  ) : null;

  if (hasError) {
    return (
      <Alert variant="destructive">
        <AlertTitle>
          {complete
            ? "Could not refresh results"
            : "Results download incomplete"}
        </AlertTitle>
        <AlertDescription className="flex flex-wrap items-center gap-2">
          {complete && <span>Showing the last complete results.</span>}
          {counts}
          <Button
            type="button"
            variant="secondary"
            size="sm"
            className="h-7"
            onClick={onRetry}
            disabled={isLoading}
          >
            {isLoading ? "Retrying…" : "Retry"}
          </Button>
        </AlertDescription>
      </Alert>
    );
  }

  return (
    <div
      role="status"
      className="text-muted-foreground flex flex-wrap gap-x-2 gap-y-1 text-xs"
    >
      <span>
        {complete
          ? isLoading
            ? "Refreshing results…"
            : "All results loaded."
          : "Downloading results…"}
      </span>
      {counts}
    </div>
  );
}
