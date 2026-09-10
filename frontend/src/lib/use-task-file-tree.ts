"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useAuth } from "@clerk/nextjs";
import useSWR, { preload, unstable_serialize, useSWRConfig } from "swr";
import { fetcher } from "@/lib/api";

export interface TaskFile {
  path: string;
  key: string;
  content?: string;
  size?: number;
  last_modified?: string;
  url?: string;
}

export interface FilesListingResponse {
  source_hash?: string | null;
  files?: TaskFile[];
  dirs?: Array<{ path: string }>;
  cursor?: string | null;
}

interface TaskFileTree {
  source_hash: string | null;
  directories: Record<string, FilesListingResponse>;
  fetchedAt: number;
}

// Only metadata for likely next views, never a recursive walk or file bodies.
const INITIAL_DIRECTORIES = ["", "solution", "tests", "environment"];
const FRESH_MS = 30_000;
type TreeKey = readonly [
  "task-file-tree",
  string,
  string,
  number | null,
  string | null,
];

function treeKey(
  scope: string,
  url: string,
  version: number | null,
  hash: string | null
): TreeKey {
  return ["task-file-tree", scope, url, version, hash];
}

function listingUrl(key: TreeKey, path?: string, cursor?: string | null) {
  const params = new URLSearchParams({
    recursive: "0",
    inline: "0",
    presign: "0",
    limit: "100",
  });
  if (key[3] !== null) params.set("version", String(key[3]));
  if (key[4]) params.set("source_hash", key[4]);
  if (path === undefined)
    INITIAL_DIRECTORIES.forEach((dir) => params.append("directories", dir));
  else if (path) params.set("prefix", path);
  if (cursor) params.set("cursor", cursor);
  return `${key[2]}?${params}`;
}

async function fetchTree(key: TreeKey): Promise<TaskFileTree> {
  const data = await fetcher<
    Omit<TaskFileTree, "fetchedAt"> | FilesListingResponse
  >(listingUrl(key), {
    cache: "no-store",
    signal: AbortSignal.timeout(15_000),
  });
  // During a rolling deployment an older server returns only the root page.
  // The panel can still request its other directories through the original API.
  return {
    source_hash: data.source_hash ?? null,
    directories: "directories" in data ? data.directories : { "": data },
    fetchedAt: Date.now(),
  };
}

function useTaskFileCacheScope(url: string) {
  const { userId, orgId } = useAuth();
  // Public URLs include their share token; never reuse an authenticated entry.
  return url.startsWith("/api/public/")
    ? "public"
    : userId && orgId
      ? `${userId}:${orgId}`
      : null;
}

/** Warm the same resource the panel consumes, on one task's pointer/keyboard intent. */
export function usePrefetchTaskFiles() {
  const scope = useTaskFileCacheScope("/api");
  const { cache } = useSWRConfig();
  const intentTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (intentTimer.current) clearTimeout(intentTimer.current);
    },
    [scope]
  );
  return useCallback(
    (url: string, version: number | null) => {
      if (!scope || version === null) return;
      if (intentTimer.current) clearTimeout(intentTimer.current);
      intentTimer.current = setTimeout(() => {
        const key = treeKey(scope, url, version, null);
        const entry = cache.get(unstable_serialize(key));
        const cached = entry?.data as TaskFileTree | undefined;
        if (
          entry?.isValidating ||
          (cached && Date.now() - cached.fetchedAt < FRESH_MS)
        )
          return;
        void preload(key, fetchTree).catch(() => {});
      }, 150);
    },
    [scope, cache]
  );
}

/** Own directory data and pagination; file selection and browser history belong to the page. */
export function useTaskFileTree({
  enabled,
  url,
  version,
  hash,
}: {
  enabled: boolean;
  url: string;
  version: number | null;
  hash: string | null;
}) {
  const scope = useTaskFileCacheScope(url);
  const { cache, mutate: mutateCache } = useSWRConfig();
  const key = useMemo(
    () => (enabled && scope ? treeKey(scope, url, version, hash) : null),
    [enabled, scope, url, version, hash]
  );
  const identity = unstable_serialize(key);
  const cached = cache.get(identity)?.data as TaskFileTree | undefined;
  const { data, error, isLoading, mutate } = useSWR(key, fetchTree, {
    revalidateOnMount: !cached || Date.now() - cached.fetchedAt >= FRESH_MS,
    revalidateOnFocus: false,
    shouldRetryOnError: false,
  });
  const activeRequests = useRef(new Set<string>());
  const [requests, setRequests] = useState<Record<string, "loading" | "error">>(
    {}
  );

  // A long-unconsumed hover prefetch may predate this mount. Refresh its data
  // while retaining the tree, just as for an expired ordinary cache entry.
  useEffect(() => {
    if (data && Date.now() - data.fetchedAt >= FRESH_MS) void mutate();
  }, [data, mutate]);

  // A listing may start before panel metadata supplies the content hash. Give
  // that same response its exact revision key, so remounting with known metadata
  // reuses the inventory and any loaded continuation pages.
  useEffect(() => {
    if (key && data?.source_hash && key[4] !== data.source_hash) {
      void mutateCache(
        treeKey(key[1], key[2], key[3], data.source_hash),
        data,
        { revalidate: false }
      );
    }
  }, [key, data, mutateCache]);

  const loadDirectory = useCallback(
    async (path: string | null, cursor?: string | null) => {
      if (!key || !data) return;
      const directory = path ?? "";
      const requestId = `${identity}:${directory}`;
      if (activeRequests.current.has(requestId)) return;
      activeRequests.current.add(requestId);
      setRequests((previous) => ({ ...previous, [requestId]: "loading" }));
      let failed = false;
      try {
        const page = await fetcher<FilesListingResponse>(
          listingUrl(key, directory, cursor),
          {
            cache: "no-store",
            signal: AbortSignal.timeout(15_000),
          }
        );
        if ((page.source_hash ?? null) !== data.source_hash) {
          // An overwrite happened between pages. Re-resolve the complete tree;
          // never append another revision's page to the cached inventory.
          await mutateCache(key);
          return;
        }
        await mutateCache<TaskFileTree>(
          key,
          (current) => {
            if (!current || current.source_hash !== data.source_hash)
              return current;
            const previous = cursor
              ? current.directories[directory]
              : undefined;
            return {
              ...current,
              directories: {
                ...current.directories,
                [directory]: {
                  ...page,
                  files: [
                    ...new Map(
                      [...(previous?.files ?? []), ...(page.files ?? [])].map(
                        (file) => [file.path, file]
                      )
                    ).values(),
                  ],
                  dirs: [
                    ...new Map(
                      [...(previous?.dirs ?? []), ...(page.dirs ?? [])].map(
                        (dir) => [dir.path, dir]
                      )
                    ).values(),
                  ],
                },
              },
            };
          },
          { revalidate: false }
        );
      } catch {
        failed = true;
        setRequests((previous) => ({ ...previous, [requestId]: "error" }));
      } finally {
        activeRequests.current.delete(requestId);
        if (!failed)
          setRequests((previous) => {
            const next = { ...previous };
            delete next[requestId];
            return next;
          });
      }
    },
    [key, identity, data, mutateCache]
  );

  const statusByDirectory = useMemo(
    () =>
      Object.fromEntries(
        Object.entries(requests)
          .filter(([id]) => id.startsWith(`${identity}:`))
          .map(([id, status]) => [id.slice(identity.length + 1), status])
      ),
    [requests, identity]
  );
  return { data, error, isLoading, loadDirectory, statusByDirectory, identity };
}
