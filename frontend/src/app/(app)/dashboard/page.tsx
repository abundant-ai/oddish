import { DashboardClient } from "./dashboard-client";

export default async function DashboardPage() {
  // Avoid blocking initial route render on server-side dashboard fetch.
  return <DashboardClient />;
}
