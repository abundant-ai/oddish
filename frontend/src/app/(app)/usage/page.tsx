import { redirect } from "next/navigation";
import { auth } from "@clerk/nextjs/server";
import { isOrgAdminRole } from "@/lib/org-roles";

// Usage moved into the admin dashboard; keep old bookmarks/links working for
// admins and send everyone else home instead of into the admin gate.
export default async function UsagePage() {
  const { orgRole } = await auth();
  redirect(isOrgAdminRole(orgRole) ? "/admin?tab=usage" : "/dashboard");
}
