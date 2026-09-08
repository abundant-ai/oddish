import { redirect } from "next/navigation";
import { auth } from "@clerk/nextjs/server";
import { isOrgAdminRole } from "@/lib/org-roles";

// Server-side gate for /admin and everything under it (tabs, /admin/users/*).
// Hiding the nav/dashboard links is cosmetic; this stops non-admins who type
// the URL (or follow the old /usage redirect) from rendering the admin UI at
// all. The backend still 403s its /admin/* endpoints independently.
export default async function AdminLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const { orgRole } = await auth();
  if (!isOrgAdminRole(orgRole)) {
    redirect("/dashboard");
  }
  return <>{children}</>;
}
