import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";

export function ExperimentPageLoadAlert({
  resource,
  loaded,
  total,
  isRetrying,
  onRetry,
}: {
  resource: "tasks" | "trials";
  loaded: number;
  total: number;
  isRetrying: boolean;
  onRetry: () => void;
}) {
  return (
    <Alert variant="destructive">
      <AlertTitle>
        Some {resource === "trials" ? "trial results" : "tasks"} failed to load
      </AlertTitle>
      <AlertDescription className="flex flex-wrap items-center gap-2">
        <span>
          Loaded {loaded}/{total} {resource}.
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
