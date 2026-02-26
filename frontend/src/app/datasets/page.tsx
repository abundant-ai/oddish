"use client";

import Link from "next/link";
import useSWR from "swr";
import { Card, CardContent } from "@/components/ui/card";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Nav } from "@/components/nav";
import { fetcher } from "@/lib/api";
import { formatShortDateTime } from "@/lib/utils";

type PublicDataset = {
  id: string;
  name: string;
  public_token: string;
  task_count: number;
  created_at: string;
};

export default function DatasetsLandingPage() {
  const { data, error, isLoading } = useSWR<PublicDataset[]>(
    "/api/public/experiments?limit=200",
    fetcher,
    {
      refreshInterval: 30000,
      revalidateOnFocus: false,
    },
  );
  const datasets = Array.isArray(data) ? data : [];

  return (
    <>
      <Nav />

      <main className="px-4 py-8 max-w-5xl mx-auto w-full space-y-3">
        {error && (
          <Alert variant="destructive">
            <AlertTitle>Failed to load datasets</AlertTitle>
            <AlertDescription>
              {error instanceof Error
                ? error.message
                : "Could not fetch public datasets right now."}
            </AlertDescription>
          </Alert>
        )}

        {isLoading ? (
          <div className="text-sm text-muted-foreground">
            Loading datasets...
          </div>
        ) : error ? null : datasets.length === 0 ? (
          <div className="text-sm text-muted-foreground">
            No public datasets available yet.
          </div>
        ) : (
          <div className="grid gap-3 md:grid-cols-2">
            {datasets.map((dataset) => (
              <Link
                key={dataset.public_token}
                href={`/datasets/${encodeURIComponent(dataset.public_token)}`}
                className="block"
              >
                <Card className="transition-colors hover:bg-muted/40">
                  <CardContent className="p-4">
                    <div className="space-y-3">
                      <div className="font-medium">{dataset.name}</div>
                      <div className="flex items-center justify-between gap-3">
                        <div className="text-xs text-muted-foreground">
                          Created {formatShortDateTime(dataset.created_at)}
                        </div>
                        <Badge variant="secondary">
                          {dataset.task_count} tasks
                        </Badge>
                      </div>
                    </div>
                  </CardContent>
                </Card>
              </Link>
            ))}
          </div>
        )}
      </main>
    </>
  );
}
