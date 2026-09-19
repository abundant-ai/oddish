// Only the isolated test app replaces authentication.
import { createContext, useContext } from "react";

const AuthContext = createContext({
  orgRole: "org:admin",
  isLoaded: true,
  userId: "user-1",
  orgId: "org-1",
});
export const TestAuthProvider = AuthContext.Provider;
export const useAuth = () => useContext(AuthContext);
