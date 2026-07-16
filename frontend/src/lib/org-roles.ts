// Clerk org roles that the backend maps to UserRole.ADMIN (see
// backend/auth/provisioning.py resolve_role). Keep in sync: this drives
// which admin-only UI (nav links, admin dashboard entry points) is shown.
export function isOrgAdminRole(orgRole: string | null | undefined): boolean {
  return orgRole === "org:admin" || orgRole === "org:owner";
}
