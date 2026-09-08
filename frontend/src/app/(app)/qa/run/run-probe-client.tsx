"use client";

import { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import useSWR from "swr";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { fetcher } from "@/lib/api";

type TaskOption = { id: string; name: string };

export function RunProbeClient() {
  const router = useRouter();
  const [query, setQuery] = useState("");
  const { data, error } = useSWR<TaskOption[]>("/api/tasks", fetcher);

  const filtered = useMemo(() => {
    const list = data ?? [];
    const q = query.trim().toLowerCase();
    const matches = q
      ? list.filter((t) => t.name.toLowerCase().includes(q))
      : list;
    return matches.slice(0, 50);
  }, [data, query]);

  return (
    <Card className="border-[#6f88b4]/20 shadow-xs">
      <CardHeader className="space-y-1 pb-3">
        <CardTitle className="text-base">Run probe</CardTitle>
        <p className="text-muted-foreground text-[11px]">
          Pick a task to probe. You&apos;ll land on its probe page to configure
          and launch the run.
        </p>
      </CardHeader>
      <CardContent className="space-y-3">
        <Input
          autoFocus
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search tasks…"
          className="h-8 border-[#6f88b4]/20"
        />
        <div className="max-h-[60vh] overflow-y-auto rounded-md border border-[#6f88b4]/20">
          {error ? (
            <div className="text-destructive p-3 text-xs">
              Failed to load tasks.
            </div>
          ) : !data ? (
            <div className="text-muted-foreground p-3 text-xs">Loading…</div>
          ) : filtered.length === 0 ? (
            <div className="text-muted-foreground p-3 text-xs">
              No matching tasks.
            </div>
          ) : (
            filtered.map((t) => (
              <button
                key={t.id}
                type="button"
                onClick={() => router.push(`/tasks/${t.id}/probe`)}
                className="hover:bg-muted focus-visible:bg-muted block w-full px-3 py-2 text-left text-xs focus-visible:outline-none"
              >
                {t.name}
              </button>
            ))
          )}
        </div>
        {filtered.length === 50 && (
          <p className="text-muted-foreground text-[10px]">
            Showing first 50 — type to filter.
          </p>
        )}
      </CardContent>
    </Card>
  );
}
