import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";

export function ExperimentTrialLoadAlert({
  loaded,
  total,
  isRetrying,
  onRetry,
}: {
  loaded: number;
  total: number;
  isRetrying: boolean;
  onRetry: () => void;
}) {
  return (
    <Alert variant="destructive">
      <AlertTitle>Some trial results failed to load</AlertTitle>
      <AlertDescription className="flex flex-wrap items-center gap-2">
        <span>
          Loaded {loaded}/{total} trials.
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
