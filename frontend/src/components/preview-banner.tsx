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
    <div className="border-b border-amber-400/40 bg-amber-100 px-4 py-2 text-sm text-amber-950 dark:border-amber-300/25 dark:bg-amber-500/15 dark:text-amber-100">
      <div className="mx-auto flex max-w-(--breakpoint-2xl) flex-wrap items-center gap-x-4 gap-y-1">
        <span className="font-semibold uppercase">Preview</span>
        <span>Backend: {backendLabel}</span>
        <span>Database: {databaseLabel}</span>
        {commitSha ? <span>Commit: {shortSha(commitSha)}</span> : null}
      </div>
    </div>
  );
}
