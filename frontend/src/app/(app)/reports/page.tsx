"use client";

import Link from "next/link";
import useSWR from "swr";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { fetcher } from "@/lib/api";
import type { ReportListItem } from "@/lib/types";
import { FileSpreadsheet } from "lucide-react";

export default function ReportsPage() {
  const { data, error, isLoading } = useSWR<ReportListItem[]>(
    "/api/reports?limit=100",
    fetcher,
  );

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="py-3">
          <div className="flex items-center gap-2">
            <FileSpreadsheet className="h-4 w-4 text-muted-foreground" />
            <div className="text-sm font-medium">Reports</div>
          </div>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <div className="text-sm text-muted-foreground">Loading…</div>
          ) : error ? (
            <Alert variant="destructive">
              <AlertTitle>Failed to load reports</AlertTitle>
              <AlertDescription>
                {error instanceof Error ? error.message : "Unknown error"}
              </AlertDescription>
            </Alert>
          ) : !data || data.length === 0 ? (
            <Alert>
              <AlertTitle>No reports yet</AlertTitle>
              <AlertDescription>
                Reports are synthetic shareable runs — author a YAML spec and
                create one with{" "}
                <code className="rounded bg-muted px-1 py-0.5">
                  oddish reports create --spec report.yaml
                </code>
                .
              </AlertDescription>
            </Alert>
          ) : (
            <div className="overflow-x-auto">
              <table className="min-w-full text-sm">
                <thead className="text-left">
                  <tr className="text-muted-foreground">
                    <th className="px-3 py-2 font-medium">Name</th>
                    <th className="px-3 py-2 font-medium">Grid</th>
                    <th className="px-3 py-2 font-medium">Public</th>
                    <th className="px-3 py-2 font-medium">Updated</th>
                  </tr>
                </thead>
                <tbody>
                  {data.map((item) => (
                    <tr key={item.id} className="border-t hover:bg-muted/40">
                      <td className="px-3 py-2">
                        <Link
                          href={`/reports/${item.id}`}
                          className="text-blue-400 hover:underline"
                        >
                          {item.name}
                        </Link>
                        <div className="text-xs text-muted-foreground">
                          {item.id}
                          {item.description ? ` · ${item.description}` : null}
                        </div>
                      </td>
                      <td className="px-3 py-2">
                        {item.rows_count} × {item.columns_count}
                      </td>
                      <td className="px-3 py-2">
                        {item.is_public && item.public_token ? (
                          <Link
                            href={`/share/reports/${item.public_token}`}
                            className="text-blue-400 hover:underline"
                          >
                            link
                          </Link>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </td>
                      <td className="px-3 py-2 text-xs text-muted-foreground">
                        {new Date(item.updated_at).toLocaleString()}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
