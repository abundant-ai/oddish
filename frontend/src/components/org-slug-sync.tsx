"use client";

import { useOrganization } from "@clerk/nextjs";
import { usePathname } from "next/navigation";
import { useEffect } from "react";
import { parseOrgSlug, withOrgSlug } from "@/lib/org-path";

/**
 * Open the active org's dashboard when the address bar names another org.
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
    // Match the organization switcher even if this Effect runs before its
    // navigation finishes. Resource IDs and filters belong to the old org.
    window.location.assign(withOrgSlug("/dashboard", organization.slug));
  }, [isLoaded, organization?.id, organization?.slug, pathname]);

  return null;
}
