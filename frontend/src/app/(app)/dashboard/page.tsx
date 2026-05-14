import { DashboardClient } from "./dashboard-client";

export default function DashboardPage() {
  // No SSR data fetch -- the client mounts the shell instantly and the
  // SWR hooks below fire usage + experiments requests in parallel,
  // each rendering as soon as its own response arrives.
  return <DashboardClient initialDashboardData={null} />;
}
