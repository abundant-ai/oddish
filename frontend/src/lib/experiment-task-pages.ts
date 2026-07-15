import type { Task } from "@/lib/types";

type FetchTaskPage = (
  input: RequestInfo | URL,
  init?: RequestInit
) => Promise<Response>;

/** Fetch mutable experiment rows without accepting an old browser-cache entry. */
export function fetchFreshExperimentTaskPage(
  url: string,
  request: FetchTaskPage = fetch
): Promise<Response> {
  return request(url, { credentials: "include", cache: "no-store" });
}

function hasSameVersion(shell: Task, enriched: Task): boolean {
  return (
    shell.current_version_id === enriched.current_version_id &&
    shell.current_version === enriched.current_version
  );
}

/**
 * Combine the fast task shells with progressively loaded trial pages.
 *
 * A cached trial page can briefly describe the version selected before a
 * default-version change. Keep the shell until the enriched row agrees on the
 * version so one render never labels one version with another version's trials.
 */
export function mergeExperimentTaskPages(
  shells: Task[] | undefined,
  trialPages: Task[][] | undefined
): Task[] {
  const enrichedById = new Map<string, Task>();
  for (const page of trialPages ?? []) {
    for (const task of page ?? []) {
      enrichedById.set(task.id, task);
    }
  }

  const merged: Task[] = [];
  const seenIds = new Set<string>();
  for (const shell of shells ?? []) {
    seenIds.add(shell.id);
    const enriched = enrichedById.get(shell.id);
    merged.push(enriched && hasSameVersion(shell, enriched) ? enriched : shell);
  }

  for (const [id, task] of enrichedById) {
    if (!seenIds.has(id)) merged.push(task);
  }
  return merged;
}
