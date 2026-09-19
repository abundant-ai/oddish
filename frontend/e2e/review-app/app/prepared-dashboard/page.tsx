import { Suspense } from "react";
import { DashboardClient } from "@/app/(app)/dashboard/dashboard-client";
export default function DashboardFixture() {
  return (
    <Suspense>
      <DashboardClient />
    </Suspense>
  );
}
