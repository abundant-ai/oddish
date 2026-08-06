"use client";

import { useCallback } from "react";
import { useSearchParams } from "next/navigation";
import useSWR, { useSWRConfig, type SWRResponse } from "swr";
import { parseTaskSearch } from "@/lib/tag-query";
import {
  BROWSE_FORWARD_KEYS,
  PRESET_MS,
  TASKS_PAGE_SIZE,
} from "@/lib/tasks-filters";
import type { TaskBrowseResponse } from "@/lib/types";

// Cache keys hold the *display* form of the browse state (the URL params,
// rolling presets still as tokens like created_within=7d), so one filter
// state maps to one cache entry no matter when it is fetched. The fetcher
// resolves the display form into the backend request per fetch, so every
// revalidation re-resolves rolling windows against the current time — the
// same semantics the server component had, where resolution happened per
// request.
export const BROWSE_KEY_PREFIX = "/api/tasks/browse?";

/**
 * Builds the SWR cache key for one browse state from the page URL's search
 * params. Canonical: params emit in a fixed order, the legacy `query` alias
 * collapses into `q`, and offset 0 is omitted — equivalent URLs share one
 * cache entry. Every cache write that targets the grid must build its key
 * through this same function (see useTaskBrowseRevalidate), so a write can
 * never miss the entry the hook reads.
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

/**
 * Resolves the display-form params into the backend /tasks/browse query.
 * Moved verbatim from the old server-side fetch in recent-tasks-results.tsx
 * so every filter keeps exactly its meaning; only where it runs changed.
 */
export function buildBrowseQuery(display: URLSearchParams): URLSearchParams {
  const query = new URLSearchParams();
  query.set("limit", String(TASKS_PAGE_SIZE));
  query.set(
    "offset",
    String(Math.max(Number(display.get("offset") ?? "0") || 0, 0))
  );

  // Tags are a structured filter (tags/tags_any/tags_none params), so only
  // free text + author are taken from the search box. `query` is the legacy
  // search param (e.g. worker-job deep links); `q` is the current one.
  const parsed = parseTaskSearch(
    display.get("q") ?? display.get("query") ?? ""
  );
  if (parsed.text) query.set("query", parsed.text);
  if (parsed.authors.length) query.set("author", parsed.authors.join(","));

  // Rolling "Created" preset: resolve the token to (now - window) at fetch
  // time so the window is always relative to this request — including every
  // background revalidation — not when it was picked. Resolved here —
  // `created_within` is not a backend param.
  const within = display.get("created_within");
  const presetActive = !!(within && within in PRESET_MS);
  if (presetActive) {
    const ms = PRESET_MS[within as keyof typeof PRESET_MS];
    query.set("created_after", new Date(Date.now() - ms).toISOString());
  }
  const trialFinishedWithin = display.get("trial_finished_within");
  const trialFinishedPresetActive = Boolean(
    trialFinishedWithin && trialFinishedWithin in PRESET_MS
  );
  if (trialFinishedPresetActive) {
    const ms = PRESET_MS[trialFinishedWithin as keyof typeof PRESET_MS];
    query.set("trial_finished_after", new Date(Date.now() - ms).toISOString());
  }

  for (const key of BROWSE_FORWARD_KEYS) {
    if (key === "created_within" || key === "trial_finished_within") continue;
    // A live preset owns created_after; don't let a stale absolute bound in
    // the URL / saved filter clobber the rolling window.
    if (key === "created_after" && presetActive) continue;
    if (key === "trial_finished_after" && trialFinishedPresetActive) continue;
    const value = display.get(key);
    if (value) query.set(key, value);
  }

  // `tag:` tokens typed in the search box are parsed out of `q` above, so they
  // must be forwarded too (unioned with the structured Tags-filter params).
  // Without this they were stripped from the free text AND dropped, so shared
  // URLs / saved searches with tag tokens filtered nothing. Mirrors the
  // dashboard page, which forwards the same parsed tag buckets.
  const mergeTags = (param: string, extra: string[]) => {
    if (!extra.length) return;
    const existing = query.get(param)?.split(",") ?? [];
    query.set(param, Array.from(new Set([...existing, ...extra])).join(","));
  };
  mergeTags("tags", parsed.all);
  mergeTags("tags_any", parsed.any);
  mergeTags("tags_none", parsed.none);

  return query;
}

// Staging has shown multi-second browse responses; a hung fetch should fail
// like a normal error (alert + Retry) instead of leaving the skeleton up
// forever. Generous because this is the page's one data-bearing request.
const BROWSE_FETCH_TIMEOUT_MS = 30_000;

async function browseFetcher(key: string): Promise<TaskBrowseResponse> {
  const display = new URLSearchParams(key.slice(BROWSE_KEY_PREFIX.length));
  const res = await fetch(`/api/tasks/browse?${buildBrowseQuery(display)}`, {
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
