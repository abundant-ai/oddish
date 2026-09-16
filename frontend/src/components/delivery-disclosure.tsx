"use client";

import type { ReactNode } from "react";
import { usePathname, useSearchParams } from "next/navigation";
import { deliveryViewQuery } from "@/lib/deliveries";

/** Shareable disclosures. A ! prefix records an explicit collapse of a default-open panel. */
export function DeliveryDisclosure({
  panel,
  defaultOpen = false,
  className,
  children,
}: {
  panel: string;
  defaultOpen?: boolean;
  className?: string;
  children: ReactNode;
}) {
  const params = useSearchParams();
  const pathname = usePathname();
  const panels = (params.get("panels") ?? "").split(",");
  const open =
    panels.includes(panel) || (defaultOpen && !panels.includes(`!${panel}`));
  return (
    <details
      className={className}
      open={open}
      onClick={(event) => {
        // Only intercept this disclosure's own summary, never nested evidence or links.
        const summary = (event.target as HTMLElement).closest("summary");
        if (summary?.parentElement !== event.currentTarget) return;
        event.preventDefault();
        const next = panels.filter(
          (key) => key && key !== panel && key !== `!${panel}`
        );
        if (open === defaultOpen) next.push(open ? `!${panel}` : panel);
        window.history.pushState(
          null,
          "",
          `${pathname}${deliveryViewQuery(window.location.search, { panels: next.join(",") || null })}${window.location.hash}`
        );
      }}
    >
      {children}
    </details>
  );
}
