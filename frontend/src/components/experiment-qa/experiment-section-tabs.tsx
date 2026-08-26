import Link from "next/link";
import { Globe } from "lucide-react";
import { cn } from "@/lib/utils";

export function ExperimentSectionTabs({
  active,
  experimentHref,
  qaHref,
  publicView = false,
}: {
  active: "experiment" | "qa";
  experimentHref: string;
  qaHref?: string | null;
  publicView?: boolean;
}) {
  const linkClass =
    "border-b-2 px-0.5 py-2 font-mono text-[12px] font-semibold transition-colors";

  return (
    <nav
      aria-label="Experiment sections"
      className="border-paper-line-2 flex items-center gap-4 border-b"
    >
      <Link
        href={experimentHref}
        aria-current={active === "experiment" ? "page" : undefined}
        className={cn(
          linkClass,
          active === "experiment"
            ? "border-paper-ink text-paper-ink"
            : "text-paper-ink-3 hover:text-paper-ink border-transparent"
        )}
      >
        Experiment
      </Link>
      {qaHref ? (
        <Link
          href={qaHref}
          aria-current={active === "qa" ? "page" : undefined}
          className={cn(
            linkClass,
            active === "qa"
              ? "border-paper-ink text-paper-ink"
              : "text-paper-ink-3 hover:text-paper-ink border-transparent"
          )}
        >
          QA
        </Link>
      ) : null}
      {publicView ? (
        <span className="text-paper-ink-3 ml-auto inline-flex items-center gap-1 font-mono text-[11px]">
          <Globe className="size-3" aria-hidden="true" />
          shared view
        </span>
      ) : null}
    </nav>
  );
}
