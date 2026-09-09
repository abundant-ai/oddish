"use client";

import { useCallback } from "react";
import { useSearchParams } from "next/navigation";
import useSWR, { useSWRConfig, type SWRResponse } from "swr";
import { BROWSE_FORWARD_KEYS } from "@/lib/tasks-filters";
import type {
  TaskBrowseCountResponse,
  TaskBrowseResponse,
} from "@/lib/types";

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

/**
 * The SWR key for the matching-task count of one filter state.
 *
 * Deliberately the same params as ``browseKey`` MINUS ``offset``: the count
 * describes the whole filter set, so every page of one set resolves to a
 * single cache entry and paging re-uses it instead of re-running the count.
 */
export function browseCountKey(searchParams: URLSearchParams): string {
  const params = new URLSearchParams();
  const q = searchParams.get("q") ?? searchParams.get("query");
  if (q) params.set("q", q);
  for (const key of BROWSE_FORWARD_KEYS) {
    const value = searchParams.get(key);
    if (value) params.set(key, value);
  }
  params.set("count_only", "true");
  return `${BROWSE_KEY_PREFIX}${params.toString()}`;
}

// Staging has shown multi-second browse responses; a hung fetch should fail
// like a normal error (alert + Retry) instead of leaving the skeleton up
// forever. Generous because this is the page's one data-bearing request.
const BROWSE_FETCH_TIMEOUT_MS = 30_000;

// Only one browse state is ever meaningful at a time (the grid is a
// singleton), so starting a fetch aborts the previous one. The abort reaches
// the backend: the proxy forwards its request signal upstream, so a
// superseded filter state stops consuming a proxy slot and a backend query
// instead of running to completion for a result nobody will render.
let inflight: AbortController | null = null;

let countInflight: AbortController | null = null;

async function countFetcher(key: string): Promise<TaskBrowseCountResponse> {
  countInflight?.abort();
  const controller = new AbortController();
  countInflight = controller;
  const timeout = window.setTimeout(
    () => controller.abort(),
    BROWSE_FETCH_TIMEOUT_MS
  );
  try {
    const res = await fetch(key, {
      credentials: "include",
      cache: "no-store",
      signal: controller.signal,
    });
    if (!res.ok) throw new Error(res.statusText || "Request failed");
    return (await res.json()) as TaskBrowseCountResponse;
  } finally {
    window.clearTimeout(timeout);
    if (countInflight === controller) countInflight = null;
  }
}

async function browseFetcher(key: string): Promise<TaskBrowseResponse> {
  inflight?.abort();
  const controller = new AbortController();
  inflight = controller;
  const timeout = window.setTimeout(
    () => controller.abort(),
    BROWSE_FETCH_TIMEOUT_MS
  );
  try {
    const res = await fetch(key, {
      credentials: "include",
      cache: "no-store",
      signal: controller.signal,
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
  } finally {
    window.clearTimeout(timeout);
    if (inflight === controller) inflight = null;
  }
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
      // An aborted request — a superseded filter state or the timeout —
      // must not burn the global retry budget re-running work nobody is
      // waiting for; every other error keeps the default retry behavior.
      shouldRetryOnError: (error) => error.name !== "AbortError",
    }
  );
}

/**
 * Returns a callback that revalidates the browse state currently in the
 * URL and settles when the fetch does — the toolbar's spinner tracks it.
 * Failures surface through the hook's error state (the grid's banner), so
 * the returned promise never rejects. Used by the Refresh button, its
 * auto-refresh interval, and the import dialog.
 */
export function useTaskBrowseRevalidate(): () => Promise<unknown> {
  const searchParams = useSearchParams();
  const { mutate } = useSWRConfig();
  return useCallback(() => {
    const sp = new URLSearchParams(searchParams.toString());
    // Refresh the count alongside the grid: an import or a finished run
    // changes how many tasks match, and a stale total beside fresh cards is
    // worse than no total at all.
    return Promise.all([
      mutate(browseKey(sp)),
      mutate(browseCountKey(sp)),
    ]).catch(() => undefined);
  }, [mutate, searchParams]);
}

/**
 * Fetches the matching-task count for the filter state in ``searchParams``.
 *
 * A request of its own, in parallel with the grid's: the count is the more
 * expensive half (it scans the whole filtered set, where the page stops at 24
 * rows), so pairing them would hold every page of cards behind it.
 *
 * ``keepPreviousData`` keeps the last count on screen across a filter change
 * rather than collapsing the header — but a number for the PREVIOUS filters
 * must never read as the current answer, so callers get ``isStale`` and are
 * expected to mark or hide it. A failed fetch is stale for the same reason:
 * the last good count may describe filters the user has already left.
 */
export function useTaskBrowseCount(searchParams: URLSearchParams): {
  total: number | null;
  isStale: boolean;
} {
  const { data, error, isLoading } = useSWR<TaskBrowseCountResponse, Error>(
    browseCountKey(searchParams),
    countFetcher,
    {
      revalidateOnFocus: false,
      keepPreviousData: true,
      shouldRetryOnError: (err) => err.name !== "AbortError",
    }
  );
  return {
    total: typeof data?.total === "number" ? data.total : null,
    isStale: isLoading || Boolean(error),
  };
}
