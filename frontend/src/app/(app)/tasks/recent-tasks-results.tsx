import Link from "next/link";
import { auth } from "@clerk/nextjs/server";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import { parseTaskSearch } from "@/lib/tag-query";
import {
  BROWSE_FORWARD_KEYS,
  PRESET_MS,
  TASKS_PAGE_SIZE,
} from "@/lib/tasks-filters";
import { cn } from "@/lib/utils";
import type { TaskBrowseResponse } from "@/lib/types";
import { TaskCard } from "./task-card";

type RawSearchParams = Record<string, string | string[] | undefined>;

function toUrlParams(searchParams: RawSearchParams): URLSearchParams {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(searchParams)) {
    if (typeof value === "string") params.set(key, value);
    else if (Array.isArray(value) && value.length > 0)
      params.set(key, value[0]);
  }
  return params;
}

async function fetchBrowse(
  sp: URLSearchParams,
  offset: number,
): Promise<TaskBrowseResponse | null> {
  try {
    const authObj = await auth();
    if (!authObj?.userId) return null;
    const token = await getClerkToken(authObj.getToken);
    if (!token) return null;

    const query: Record<string, string> = {
      limit: String(TASKS_PAGE_SIZE),
      offset: String(offset),
    };
    // Tags are a structured filter (tags/tags_any/tags_none params), so only
    // free text + author are taken from the search box. `query` is the legacy
    // search param (e.g. worker-job deep links); `q` is the current one.
    const parsed = parseTaskSearch(sp.get("q") ?? sp.get("query") ?? "");
    if (parsed.text) query.query = parsed.text;
    if (parsed.authors.length) query.author = parsed.authors.join(",");

    // Rolling "Created" preset: resolve the token to (now - window) at query
    // time so the window is always relative to this request, not when it was
    // picked. Resolved here — `created_within` is not a backend param.
    const within = sp.get("created_within");
    const presetActive = !!(within && within in PRESET_MS);
    if (presetActive) {
      const ms = PRESET_MS[within as keyof typeof PRESET_MS];
      query.created_after = new Date(Date.now() - ms).toISOString();
    }
    const trialFinishedWithin = sp.get("trial_finished_within");
    const trialFinishedPresetActive = Boolean(
      trialFinishedWithin && trialFinishedWithin in PRESET_MS,
    );
    if (trialFinishedPresetActive) {
      const ms =
        PRESET_MS[trialFinishedWithin as keyof typeof PRESET_MS];
      query.trial_finished_after = new Date(Date.now() - ms).toISOString();
    }

    for (const key of BROWSE_FORWARD_KEYS) {
      if (key === "created_within" || key === "trial_finished_within") continue;
      // A live preset owns created_after; don't let a stale absolute bound in
      // the URL / saved filter clobber the rolling window.
      if (key === "created_after" && presetActive) continue;
      if (
        key === "trial_finished_after" &&
        trialFinishedPresetActive
      )
        continue;
      const value = sp.get(key);
      if (value) query[key] = value;
    }

    // `tag:` tokens typed in the search box are parsed out of `q` above, so they
    // must be forwarded too (unioned with the structured Tags-filter params).
    // Without this they were stripped from the free text AND dropped, so shared
    // URLs / saved searches with tag tokens filtered nothing. Mirrors the
    // dashboard page, which forwards the same parsed tag buckets.
    const mergeTags = (param: string, extra: string[]) => {
      if (!extra.length) return;
      const existing = query[param] ? query[param].split(",") : [];
      query[param] = Array.from(new Set([...existing, ...extra])).join(",");
    };
    mergeTags("tags", parsed.all);
    mergeTags("tags_any", parsed.any);
    mergeTags("tags_none", parsed.none);

    const res = await fetch(getBackendUrl("tasks/browse", "", query), {
      cache: "no-store",
      headers: getAuthHeaders(token),
    });
    if (!res.ok) {
      console.error(`[tasks/results] Backend error: ${res.status}`);
      return null;
    }
    return (await res.json()) as TaskBrowseResponse;
  } catch (error) {
    console.error("[tasks/results] fetch failed", error);
    return null;
  }
}

function pageHref(sp: URLSearchParams, offset: number): string {
  const params = new URLSearchParams(sp.toString());
  if (offset <= 0) params.delete("offset");
  else params.set("offset", String(offset));
  const query = params.toString();
  return query ? `/tasks?${query}` : "/tasks";
}

const pagerClass =
  "inline-flex h-8 items-center gap-1 rounded-md border border-[#6f88b4]/30 px-3 text-[11px] transition-colors";

export async function RecentTasksResults({
  searchParams,
}: {
  searchParams: RawSearchParams;
}) {
  const sp = toUrlParams(searchParams);
  const offset = Math.max(Number(sp.get("offset") ?? "0") || 0, 0);
  const data = await fetchBrowse(sp, offset);

  if (!data) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Failed to load tasks</AlertTitle>
        <AlertDescription>
          Check the API connection and try again.
        </AlertDescription>
      </Alert>
    );
  }

  const items = data.items ?? [];
  const hasMore = data.has_more ?? false;

  if (items.length === 0) {
    return (
      <div className="bg-card/60 text-muted-foreground rounded-lg border border-dashed border-[#6f88b4]/30 px-6 py-10 text-center text-sm">
        No tasks match the current filters.
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {items.map((task) => (
          <TaskCard key={task.id} task={task} />
        ))}
      </div>

      <div className="flex items-center justify-between gap-2">
        <div className="text-muted-foreground text-xs">
          {offset + 1}-{offset + items.length} shown
        </div>
        <div className="flex items-center gap-2">
          {offset === 0 ? (
            <span
              className={cn(
                pagerClass,
                "text-muted-foreground/50 cursor-not-allowed",
              )}
              aria-disabled
            >
              <ChevronLeft className="h-3.5 w-3.5" />
              Previous page
            </span>
          ) : (
            <Link
              href={pageHref(sp, offset - TASKS_PAGE_SIZE)}
              scroll={false}
              className={cn(pagerClass, "hover:bg-muted")}
            >
              <ChevronLeft className="h-3.5 w-3.5" />
              Previous page
            </Link>
          )}
          {hasMore ? (
            <Link
              href={pageHref(sp, offset + TASKS_PAGE_SIZE)}
              scroll={false}
              className={cn(pagerClass, "hover:bg-muted")}
            >
              Next page
              <ChevronRight className="h-3.5 w-3.5" />
            </Link>
          ) : (
            <span
              className={cn(
                pagerClass,
                "text-muted-foreground/50 cursor-not-allowed",
              )}
              aria-disabled
            >
              Next page
              <ChevronRight className="h-3.5 w-3.5" />
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
