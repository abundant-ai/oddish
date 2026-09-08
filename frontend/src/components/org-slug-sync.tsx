"use client";

import { useOrganization } from "@clerk/nextjs";
import { usePathname } from "next/navigation";
import { useEffect } from "react";
import { parseOrgSlug, stripOrgSlug, withOrgSlug } from "@/lib/org-path";

/**
 * Keep the address bar on the active org's slug.
 *
 * Org-scoped SWR keys and Next's client router cache are keyed on the URL, so
 * a soft navigation after an org switch can show the previous workspace. A
 * hard assign drops that cache the same way the old `/dashboard` reload did.
 */
export function OrgSlugSync() {
  const pathname = usePathname();
  const { organization, isLoaded } = useOrganization();

  useEffect(() => {
    if (!isLoaded || !organization?.slug) return;
    const urlSlug = parseOrgSlug(pathname);
    // Unprefixed /tasks stays for middleware to 307. Only a *wrong* slug
    // in the address bar needs a hard load (org switch / pasted URL).
    if (!urlSlug || urlSlug === organization.slug) return;
    const appPath = stripOrgSlug(pathname);
    const dest = withOrgSlug(
      appPath === "/" ? "/dashboard" : appPath,
      organization.slug,
    );
    window.location.assign(
      `${dest}${window.location.search}${window.location.hash}`,
    );
  }, [isLoaded, organization?.id, organization?.slug, pathname]);

  return null;
}
