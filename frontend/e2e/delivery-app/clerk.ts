// Only the isolated test app replaces authentication; production routes are unchanged.
export const useAuth = () => ({ orgRole: "org:admin" });
