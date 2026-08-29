"use client";

import { useEffect, useRef } from "react";

export function ExperimentPaginationSentinel({
  hasMoreTasks,
  hasMoreTrials,
  loadNextTasks,
  loadNextTrials,
}: {
  hasMoreTasks: boolean;
  hasMoreTrials: boolean;
  loadNextTasks: () => void;
  loadNextTrials: () => void;
}) {
  const sentinelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const sentinel = sentinelRef.current;
    if (!sentinel || (!hasMoreTasks && !hasMoreTrials)) return;

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (!entry?.isIntersecting) return;
        if (hasMoreTasks) loadNextTasks();
        if (hasMoreTrials) loadNextTrials();
      },
      { rootMargin: "400px 0px" }
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [hasMoreTasks, hasMoreTrials, loadNextTasks, loadNextTrials]);

  return <div ref={sentinelRef} className="h-px" aria-hidden="true" />;
}
