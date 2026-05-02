import Link from "next/link";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Beaker } from "lucide-react";

export default function ExperimentsPage() {
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="py-3">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex items-center gap-2">
              <Beaker className="text-muted-foreground h-4 w-4" />
              <div className="text-sm font-medium">Experiments</div>
            </div>
            <Button asChild size="sm">
              <Link href="/experiments/new">Build experiment</Link>
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          <Alert>
            <AlertTitle>Build or select an experiment</AlertTitle>
            <AlertDescription>
              Create a task-version matrix, or open an existing experiment from
              the dashboard.{" "}
              <Link
                href="/dashboard"
                className="text-blue-400 hover:text-blue-300 hover:underline"
              >
                Go to dashboard
              </Link>
              .
            </AlertDescription>
          </Alert>
        </CardContent>
      </Card>
    </div>
  );
}
