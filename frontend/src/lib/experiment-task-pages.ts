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

/** Only replace the experiment grid when a failed request left no usable rows. */
export function hasFatalExperimentTaskLoadError(
  error: unknown,
  tasks: Task[] | undefined
): boolean {
  return Boolean(error) && (tasks?.length ?? 0) === 0;
}

function hasSameVersion(shell: Task, enriched: Task): boolean {
  return (
    shell.current_version_id === enriched.current_version_id &&
    shell.current_version === enriched.current_version &&
    shell.trial_version_id === enriched.trial_version_id &&
    shell.trial_version === enriched.trial_version
  );
}

function preferEnrichedVersion(shell: Task, enriched: Task): boolean {
  if (hasSameVersion(shell, enriched)) return true;

  const shellRevision = Date.parse(shell.updated_at);
  const enrichedRevision = Date.parse(enriched.updated_at);
  return Number.isFinite(shellRevision) && enrichedRevision > shellRevision;
}

/**
 * Combine the fast task shells with progressively loaded trial pages.
 *
 * The two phases can revalidate in either order after a default-version or
 * experiment trial-pivot change. When their versions disagree, the task row's
 * database revision says which response observed the newer state. Missing or
 * tied revisions retain the conservative shell-first behavior.
 */
export function mergeExperimentTaskPages(
  shells: Task[] | undefined,
  trialPages: Task[][] | undefined
): Task[] {
  const enrichedById = new Map<string, Task>();
  for (const page of trialPages ?? []) {
    for (const task of page ?? []) {
      const previous = enrichedById.get(task.id);
      if (!previous) {
        enrichedById.set(task.id, task);
        continue;
      }
      if (!hasSameVersion(previous, task)) {
        enrichedById.set(
          task.id,
          preferEnrichedVersion(previous, task) ? task : previous,
        );
        continue;
      }
      const trialsById = new Map(
        (previous.trials ?? []).map((trial) => [trial.id, trial]),
      );
      for (const trial of task.trials ?? []) trialsById.set(trial.id, trial);
      enrichedById.set(task.id, {
        ...previous,
        ...task,
        user_tags:
          (task.user_tags?.length ?? 0) > 0
            ? task.user_tags
            : previous.user_tags,
        trials: [...trialsById.values()],
      });
    }
  }

  const merged: Task[] = [];
  const seenIds = new Set<string>();
  for (const shell of shells ?? []) {
    seenIds.add(shell.id);
    const enriched = enrichedById.get(shell.id);
    if (!enriched || !preferEnrichedVersion(shell, enriched)) {
      merged.push(shell);
      continue;
    }
    const needsShellTags =
      (shell.user_tags?.length ?? 0) > 0 &&
      (enriched.user_tags?.length ?? 0) === 0;
    merged.push(
      needsShellTags
        ? { ...shell, ...enriched, user_tags: shell.user_tags }
        : enriched,
    );
  }

  for (const [id, task] of enrichedById) {
    if (!seenIds.has(id)) merged.push(task);
  }
  return merged;
}
