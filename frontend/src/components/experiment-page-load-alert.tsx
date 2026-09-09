import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";

export function ExperimentPageLoadAlert({
  resource,
  description,
  loaded,
  total,
  isRetrying,
  onRetry,
}: {
  resource: "tasks" | "trials" | "experiment" | "dataset";
  description?: string;
  loaded: number;
  total: number;
  isRetrying: boolean;
  onRetry: () => void;
}) {
  const isFatal = resource === "experiment" || resource === "dataset";
  return (
    <Alert variant="destructive">
      <AlertTitle>
        {isFatal
          ? `Failed to load ${resource}`
          : `Some ${resource === "trials" ? "trial results" : "tasks"} failed to load`}
      </AlertTitle>
      <AlertDescription className="flex flex-wrap items-center gap-2">
        <span>
          {isFatal
            ? (description ?? "Check the API connection and try again.")
            : `Loaded ${loaded}/${total} ${resource}.`}
        </span>
        <Button
          type="button"
          variant="secondary"
          size="sm"
          className="h-7"
          onClick={onRetry}
          disabled={isRetrying}
        >
          Retry
        </Button>
      </AlertDescription>
    </Alert>
  );
}
