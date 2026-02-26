"use client";

import Link from "next/link";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Beaker } from "lucide-react";

export default function ExperimentsPage() {
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="py-3">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex items-center gap-2">
              <Beaker className="h-4 w-4 text-muted-foreground" />
              <div className="text-sm font-medium">Experiments</div>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          <Alert>
            <AlertTitle>Select an experiment</AlertTitle>
            <AlertDescription>
              Open an experiment from the dashboard to view its trials.{" "}
              <Link
                href="/"
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
