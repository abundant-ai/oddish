// Separate local fixture app; production authentication is unchanged.
export const useAuth = () => ({
  orgRole: "org:admin",
  userId: "maya",
  orgId: "fixture",
});
export const useOrganization = () => ({ organization: null, isLoaded: true });
