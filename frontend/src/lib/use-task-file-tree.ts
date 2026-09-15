"use client";

import {
  useCallback,
  useEffect,
  useEffectEvent,
  useMemo,
  useRef,
  useState,
} from "react";
import { useFileCacheScope } from "@/lib/file-resources";
import useSWR, { preload, unstable_serialize, useSWRConfig } from "swr";
import { fetcher } from "@/lib/api";

export interface TaskFile {
  path: string;
  key: string;
  size?: number;
  last_modified?: string;
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

// Bounded directory pages; never a recursive walk.
const INITIAL_DIRECTORIES = ["", "solution", "tests", "environment"];
const FRESH_MS = 30_000;
type TreeKey = readonly [
  "task-file-tree",
  string,
  string,
  number | null,
  string | null,
  number,
  boolean,
];

function treeKey(
  scope: string,
  url: string,
  version: number | null,
  hash: string | null,
  attempt = 0,
  artifacts = false
): TreeKey {
  return ["task-file-tree", scope, url, version, hash, attempt, artifacts];
}

function listingUrl(
  key: TreeKey,
  path?: string,
  cursor?: string | null,
  revision?: string | null
) {
  const params = new URLSearchParams({
    recursive: "0",
    inline: "0",
    presign: "0",
    limit: "100",
    indexed: "true",
  });
  if (key[2].includes("/trials/")) params.set("attempt", String(key[5]));
  if (key[3] !== null) params.set("version", String(key[3]));
  if (key[4]) params.set("source_hash", key[4]);
  if (revision && key[2].includes("/trials/")) params.set("revision", revision);
  if (key[6]) {
    params.set("artifacts", "true");
  } else if (path === undefined) {
    INITIAL_DIRECTORIES.forEach((dir) => params.append("directories", dir));
  } else if (path) params.set("prefix", path);
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

/** Warm the same resource the panel consumes, on one task's pointer/keyboard intent. */
export function usePrefetchTaskFiles() {
  const scope = useFileCacheScope("/api");
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
  active = enabled,
  url,
  version,
  hash,
  attempt = 0,
  artifacts = false,
}: {
  enabled: boolean;
  active?: boolean;
  url: string;
  version: number | null;
  hash: string | null;
  attempt?: number;
  artifacts?: boolean;
}) {
  const scope = useFileCacheScope(url);
  const { cache, mutate: mutateCache } = useSWRConfig();
  const key = useMemo(
    () =>
      enabled && scope
        ? treeKey(scope, url, version, hash, attempt, artifacts)
        : null,
    [enabled, scope, url, version, hash, attempt, artifacts]
  );
  const identity = unstable_serialize(key);
  const cached = cache.get(identity)?.data as TaskFileTree | undefined;
  const { data, error, isLoading, isValidating, mutate } = useSWR(
    key,
    async (key) => {
      const fresh = await fetchTree(key);
      const previous = cache.get(unstable_serialize(key))?.data as
        | TaskFileTree
        | undefined;
      // A revision identifies immutable inventory metadata. Preserve already
      // loaded continuation pages only when the server confirms that revision.
      return fresh.source_hash && fresh.source_hash === previous?.source_hash
        ? {
            ...fresh,
            directories: { ...fresh.directories, ...previous.directories },
          }
        : fresh;
    },
    {
      revalidateOnMount: !cached || Date.now() - cached.fetchedAt >= FRESH_MS,
      revalidateOnFocus: false,
      revalidateIfStale: false,
      shouldRetryOnError: (error: { status?: number }) => error.status === 503,
      errorRetryInterval: 2_000,
      errorRetryCount: 30,
    }
  );
  const requestScope = JSON.stringify([identity, data?.source_hash]);
  const activeRequests = useRef(new Set<string>());
  const [requests, setRequests] = useState<Record<string, "loading" | "error">>(
    {}
  );

  // Trial uploads can replace even a successful empty inventory. Retained
  // panes must refresh on activation, not just on their first mount. Task
  // prefetches keep their existing 30-second freshness window.
  const refreshOnEntry = useEffectEvent(() => {
    if (
      error ||
      (data &&
        (url.includes("/trials/") || Date.now() - data.fetchedAt >= FRESH_MS))
    )
      void mutate();
  });
  useEffect(() => {
    if (active) refreshOnEntry();
  }, [active, identity]);

  const onFileError = useCallback(
    async (error: { status?: number }) => {
      if (error.status === 409) await mutate();
    },
    [mutate]
  );

  // A listing may start before panel metadata supplies the content hash. Give
  // that same response its exact revision key, so remounting with known metadata
  // reuses the inventory and any loaded continuation pages.
  useEffect(() => {
    if (
      key &&
      !url.includes("/trials/") &&
      data?.source_hash &&
      key[4] !== data.source_hash
    ) {
      void mutateCache(
        treeKey(key[1], key[2], key[3], data.source_hash, key[5], key[6]),
        data,
        { revalidate: false }
      );
    }
  }, [key, data, mutateCache, url]);

  const loadDirectory = useCallback(
    async (path: string | null, cursor?: string | null) => {
      if (!key || !data) return;
      const directory = path ?? "";
      const requestId = `${requestScope}:${directory}`;
      if (activeRequests.current.has(requestId)) return;
      activeRequests.current.add(requestId);
      setRequests((previous) => ({ ...previous, [requestId]: "loading" }));
      let failed = false;
      try {
        const page = await fetcher<FilesListingResponse>(
          listingUrl(key, directory, cursor, data.source_hash),
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
      } catch (error) {
        if ((error as { status?: number }).status === 409) {
          await mutateCache(key);
        } else {
          failed = true;
          setRequests((previous) => ({ ...previous, [requestId]: "error" }));
        }
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
    [key, requestScope, data, mutateCache]
  );

  const statusByDirectory = useMemo(
    () =>
      Object.fromEntries(
        Object.entries(requests)
          .filter(([id]) => id.startsWith(`${requestScope}:`))
          .map(([id, status]) => [id.slice(requestScope.length + 1), status])
      ),
    [requests, requestScope]
  );
  return {
    data,
    error,
    isLoading,
    isValidating,
    loadDirectory,
    statusByDirectory,
    identity,
    refresh: mutate,
    onFileError,
  };
}
