import { auth } from "@clerk/nextjs/server";
import { orgRedirect } from "@/lib/org-redirect";
import { isOrgAdminRole } from "@/lib/org-roles";

// Usage moved into the admin dashboard; keep old bookmarks/links working for
// admins and send everyone else home instead of into the admin gate.
export default async function UsagePage() {
  const { orgRole } = await auth();
  await orgRedirect(isOrgAdminRole(orgRole) ? "/admin?tab=usage" : "/dashboard");
}
