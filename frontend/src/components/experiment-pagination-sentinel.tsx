"use client";

import { useEffect, useRef } from "react";

export function ExperimentPaginationSentinel({
  hasMoreTasks,
  loadNextTasks,
}: {
  hasMoreTasks: boolean;
  loadNextTasks: () => void;
}) {
  const sentinelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const sentinel = sentinelRef.current;
    if (!sentinel || !hasMoreTasks) return;

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (!entry?.isIntersecting) return;
        loadNextTasks();
      },
      { rootMargin: "400px 0px" }
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [hasMoreTasks, loadNextTasks]);

  return <div ref={sentinelRef} className="h-px" aria-hidden="true" />;
}
