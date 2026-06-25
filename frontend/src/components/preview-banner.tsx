const linkClass = "underline underline-offset-2 hover:no-underline";

function shortSha(value: string) {
  return value.length > 7 ? value.slice(0, 7) : value;
}

function prNumberFromUrl(url: string) {
  const match = url.match(/\/pull\/(\d+)/);
  return match ? match[1] : null;
}

function LabeledTarget({
  label,
  value,
  url,
}: {
  label: string;
  value: string;
  url: string;
}) {
  return (
    <span className="truncate">
      {label}:{" "}
      {url ? (
        <a
          href={url}
          target="_blank"
          rel="noopener noreferrer"
          className={linkClass}
        >
          {value}
        </a>
      ) : (
        value
      )}
    </span>
  );
}

export function PreviewBanner() {
  const isPreview = process.env.NEXT_PUBLIC_ODDISH_PREVIEW === "true";

  if (!isPreview) {
    return null;
  }

  const backendLabel =
    process.env.NEXT_PUBLIC_ODDISH_PREVIEW_BACKEND_LABEL || "unknown backend";
  const backendUrl = process.env.NEXT_PUBLIC_ODDISH_PREVIEW_BACKEND_URL || "";
  const databaseLabel =
    process.env.NEXT_PUBLIC_ODDISH_PREVIEW_DATABASE_LABEL || "unknown database";
  const databaseUrl = process.env.NEXT_PUBLIC_ODDISH_PREVIEW_DATABASE_URL || "";
  const commitSha = process.env.NEXT_PUBLIC_ODDISH_PREVIEW_COMMIT_SHA || "";
  const prUrl = process.env.NEXT_PUBLIC_ODDISH_PREVIEW_PR_URL || "";
  const prTitle = process.env.NEXT_PUBLIC_ODDISH_PREVIEW_PR_TITLE || "";
  const prNumber = prUrl ? prNumberFromUrl(prUrl) : null;
  const prLabel = prNumber ? `PR #${prNumber}` : "View PR";

  return (
    <div className="sticky top-0 z-50 h-[var(--preview-banner-h)] border-b border-amber-400/40 bg-amber-100 text-amber-950 dark:border-amber-300/25 dark:bg-amber-500/15 dark:text-amber-100">
      <div className="mx-auto flex h-full max-w-(--breakpoint-2xl) items-center gap-x-3 overflow-hidden px-4 text-[11px] leading-none whitespace-nowrap">
        <span className="font-semibold tracking-wider uppercase">Preview</span>
        <LabeledTarget label="Backend" value={backendLabel} url={backendUrl} />
        <LabeledTarget label="DB" value={databaseLabel} url={databaseUrl} />
        {prUrl ? (
          <a
            href={prUrl}
            target="_blank"
            rel="noopener noreferrer"
            className={`min-w-0 truncate font-medium ${linkClass}`}
          >
            {prTitle ? `${prLabel}: ${prTitle}` : prLabel}
          </a>
        ) : null}
        {commitSha ? (
          <span className="truncate">Commit: {shortSha(commitSha)}</span>
        ) : null}
      </div>
    </div>
  );
}
