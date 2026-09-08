"use client";

import Link from "next/link";
import type { ComponentProps } from "react";
import { useOrgHref } from "@/lib/use-org-href";

type AppLinkProps = ComponentProps<typeof Link>;

function prefixHref(
  href: AppLinkProps["href"],
  orgHref: (href: string) => string,
): AppLinkProps["href"] {
  if (typeof href === "string") return orgHref(href);
  if (href && typeof href === "object" && href.pathname) {
    return { ...href, pathname: orgHref(href.pathname) };
  }
  return href;
}

/** Drop-in `Link` that prefixes authenticated app paths with the org slug. */
export function AppLink({ href, ...props }: AppLinkProps) {
  const orgHref = useOrgHref();
  return <Link href={prefixHref(href, orgHref)} {...props} />;
}
