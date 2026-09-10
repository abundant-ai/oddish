"use client";

import { useOrganization } from "@clerk/nextjs";
import { usePathname } from "next/navigation";
import { useCallback } from "react";
import { parseOrgSlug, stripOrgSlug, withOrgSlug } from "./org-path";

export function useOrgSlug(): string | null {
  const pathname = usePathname();
  const { organization } = useOrganization();
  return organization?.slug ?? parseOrgSlug(pathname);
}

export function useOrgHref() {
  const slug = useOrgSlug();
  return useCallback((href: string) => withOrgSlug(href, slug), [slug]);
}

export function useAppPathname(): string {
  return stripOrgSlug(usePathname());
}
