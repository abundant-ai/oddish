import { auth } from "@clerk/nextjs/server";

import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import type { DeliveryListItem } from "@/lib/types";
import { DeliveriesClient } from "./deliveries-client";

// auth() reads request headers, so this page can never be prerendered.
export const dynamic = "force-dynamic";

// Server-render the list so first paint is complete; the client component
// keeps it fresh via SWR (fallbackData) and handles the create dialog.
async function getInitialDeliveries(): Promise<DeliveryListItem[] | null> {
  try {
    const authObj = await auth();
    if (!authObj?.userId) return null;
    const token = await getClerkToken(authObj.getToken);
    if (!token) return null;
    const response = await fetch(getBackendUrl("deliveries"), {
      cache: "no-store",
      headers: getAuthHeaders(token),
    });
    if (!response.ok) {
      console.error(
        `[deliveries/page] Failed initial fetch: ${response.status}`
      );
      return null;
    }
    return (await response.json()) as DeliveryListItem[];
  } catch (error) {
    console.error("[deliveries/page] Initial fetch failed", error);
    return null;
  }
}

export default async function DeliveriesPage() {
  const initialDeliveries = await getInitialDeliveries();
  return <DeliveriesClient initialDeliveries={initialDeliveries} />;
}
