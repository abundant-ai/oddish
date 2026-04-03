import { Loader2 } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";

export default function ExperimentDetailLoading() {
  return (
    <div>
      <Card>
        <CardContent className="flex min-h-[240px] items-center justify-center py-10">
          <div className="inline-flex items-center gap-2 rounded-md border border-border/70 bg-muted/40 px-3 py-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            <span>Loading experiment...</span>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
