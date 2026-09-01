import { auth } from "@clerk/nextjs/server";

import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import type { DeliveryBoardResponse } from "@/lib/types";
import { DeliveryBoardClient } from "./delivery-board-client";

// Server-render the board so first paint is complete; the client component
// keeps it fresh via SWR (fallbackData) and drives the mutations.
async function getInitialBoard(
  deliveryId: string
): Promise<DeliveryBoardResponse | null> {
  try {
    const authObj = await auth();
    if (!authObj?.userId) return null;
    const token = await getClerkToken(authObj.getToken);
    if (!token) return null;
    const response = await fetch(
      getBackendUrl("deliveries", `/${encodeURIComponent(deliveryId)}`),
      { cache: "no-store", headers: getAuthHeaders(token) }
    );
    if (!response.ok) {
      console.error(
        `[deliveries/[delivery]/page] Failed initial fetch: ${response.status}`
      );
      return null;
    }
    return (await response.json()) as DeliveryBoardResponse;
  } catch (error) {
    console.error("[deliveries/[delivery]/page] Initial fetch failed", error);
    return null;
  }
}

export default async function DeliveryPage({
  params,
}: {
  params: Promise<{ delivery: string }>;
}) {
  const { delivery } = await params;
  const initialBoard = await getInitialBoard(delivery);
  return (
    <DeliveryBoardClient deliveryId={delivery} initialBoard={initialBoard} />
  );
}
