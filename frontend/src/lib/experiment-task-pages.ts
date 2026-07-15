import type { Task } from "@/lib/types";

type FetchTaskPage = (
  input: RequestInfo | URL,
  init?: RequestInit
) => Promise<Response>;

const taskSnapshotSequence = new WeakMap<Task, number>();
let nextTaskSnapshotSequence = 1;

/** Record when a page became available so version conflicts prefer newer data. */
export function markExperimentTaskPageFetched(tasks: Task[]): Task[] {
  let sequence: number | undefined;
  for (const task of tasks) {
    if (!taskSnapshotSequence.has(task)) {
      sequence ??= nextTaskSnapshotSequence++;
      taskSnapshotSequence.set(task, sequence);
    }
  }
  return tasks;
}

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

function preferEnrichedVersion(shell: Task, enriched: Task): boolean {
  if (hasSameVersion(shell, enriched)) return true;

  const shellSequence = taskSnapshotSequence.get(shell);
  const enrichedSequence = taskSnapshotSequence.get(enriched);
  return (
    shellSequence !== undefined &&
    enrichedSequence !== undefined &&
    enrichedSequence > shellSequence
  );
}

/**
 * Combine the fast task shells with progressively loaded trial pages.
 *
 * The two phases can revalidate in either order after a default-version
 * change. When their versions disagree, use the snapshot that arrived later so
 * neither a stale shell nor a stale trial page can hide the newly selected
 * version. Unmarked callers retain the conservative shell-first behavior.
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
    merged.push(
      enriched && preferEnrichedVersion(shell, enriched) ? enriched : shell
    );
  }

  for (const [id, task] of enrichedById) {
    if (!seenIds.has(id)) merged.push(task);
  }
  return merged;
}
