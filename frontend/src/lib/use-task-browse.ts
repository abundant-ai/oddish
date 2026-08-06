"use client";

import { useCallback } from "react";
import { useSearchParams } from "next/navigation";
import useSWR, { useSWRConfig, type SWRResponse } from "swr";
import { BROWSE_FORWARD_KEYS } from "@/lib/tasks-filters";
import type { TaskBrowseResponse } from "@/lib/types";

// The SWR key is also the URL that gets fetched, in display form: the page
// URL's filter params with rolling presets still as tokens (e.g.
// created_within=7d), so one filter state maps to one cache entry no matter
// when it is fetched. The proxy (app/api/tasks/browse/route.ts) resolves the
// display form into the backend query per request, so every revalidation
// re-resolves rolling windows against the current time — the same semantics
// the grid had while it was server-rendered.
const BROWSE_KEY_PREFIX = "/api/tasks/browse?";

/**
 * Builds the SWR cache key — and fetch URL — for one browse state from the
 * page URL's search params. Canonical: params emit in a fixed order, the
 * legacy `query` alias collapses into `q`, and offset 0 is omitted —
 * equivalent URLs share one cache entry. Every cache write that targets the
 * grid must build its key through this same function (see
 * useTaskBrowseRevalidate), so a write can never miss the entry the hook
 * reads.
 */
export function browseKey(searchParams: URLSearchParams): string {
  const params = new URLSearchParams();
  const q = searchParams.get("q") ?? searchParams.get("query");
  if (q) params.set("q", q);
  for (const key of BROWSE_FORWARD_KEYS) {
    const value = searchParams.get(key);
    if (value) params.set(key, value);
  }
  const offset = Math.max(Number(searchParams.get("offset") ?? "0") || 0, 0);
  if (offset > 0) params.set("offset", String(offset));
  return `${BROWSE_KEY_PREFIX}${params.toString()}`;
}

// Staging has shown multi-second browse responses; a hung fetch should fail
// like a normal error (alert + Retry) instead of leaving the skeleton up
// forever. Generous because this is the page's one data-bearing request.
const BROWSE_FETCH_TIMEOUT_MS = 30_000;

async function browseFetcher(key: string): Promise<TaskBrowseResponse> {
  const res = await fetch(key, {
    credentials: "include",
    cache: "no-store",
    signal: AbortSignal.timeout(BROWSE_FETCH_TIMEOUT_MS),
  });
  let data: unknown = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (!res.ok) {
    const message =
      typeof data === "object" && data && "error" in data
        ? String((data as { error?: string }).error)
        : res.statusText || "Request failed";
    throw new Error(message);
  }
  return data as TaskBrowseResponse;
}

/**
 * Fetches one page of /tasks/browse for the given URL search params.
 *
 * One fetch per filter state: every mount (and revisit) that resolves the
 * same params shares one request and one cached copy. Return visits render
 * the cached grid immediately and revalidate in the background; filter and
 * pager changes keep the previous grid on screen while the next state
 * loads (keepPreviousData).
 */
export function useTaskBrowse(
  searchParams: URLSearchParams
): SWRResponse<TaskBrowseResponse, Error> {
  return useSWR<TaskBrowseResponse, Error>(
    browseKey(searchParams),
    browseFetcher,
    {
      revalidateOnFocus: false,
      keepPreviousData: true,
    }
  );
}

/**
 * Returns a callback that revalidates the browse state currently in the
 * URL — the client-side equivalent of what router.refresh() did for the
 * grid while it was server-rendered. Used by the toolbar's Refresh button,
 * its auto-refresh interval, and the import dialog.
 */
export function useTaskBrowseRevalidate(): () => void {
  const searchParams = useSearchParams();
  const { mutate } = useSWRConfig();
  return useCallback(() => {
    void mutate(browseKey(new URLSearchParams(searchParams.toString())));
  }, [mutate, searchParams]);
}
