function shortSha(value: string) {
  return value.length > 7 ? value.slice(0, 7) : value;
}

export function PreviewBanner() {
  const isPreview = process.env.NEXT_PUBLIC_ODDISH_PREVIEW === "true";

  if (!isPreview) {
    return null;
  }

  const backendLabel =
    process.env.NEXT_PUBLIC_ODDISH_PREVIEW_BACKEND_LABEL || "unknown backend";
  const databaseLabel =
    process.env.NEXT_PUBLIC_ODDISH_PREVIEW_DATABASE_LABEL || "unknown database";
  const commitSha = process.env.NEXT_PUBLIC_ODDISH_PREVIEW_COMMIT_SHA || "";

  return (
    <div className="sticky top-0 z-50 h-[var(--preview-banner-h)] border-b border-amber-400/40 bg-amber-100 text-amber-950 dark:border-amber-300/25 dark:bg-amber-500/15 dark:text-amber-100">
      <div className="mx-auto flex h-full max-w-(--breakpoint-2xl) items-center gap-x-3 overflow-hidden px-4 text-[11px] leading-none whitespace-nowrap">
        <span className="font-semibold tracking-wider uppercase">Preview</span>
        <span className="truncate">Backend: {backendLabel}</span>
        <span className="truncate">DB: {databaseLabel}</span>
        {commitSha ? (
          <span className="truncate">Commit: {shortSha(commitSha)}</span>
        ) : null}
      </div>
    </div>
  );
}
