import { deliveryPageQuery } from "@/lib/deliveries";
import { auth } from "@clerk/nextjs/server";

import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import {
  DeliveryBoardClient,
  type InitialDeliveryBoard,
} from "./delivery-board-client";

// Server-render the requested page and seed its exact browser cache key.
// The client keeps it fresh via SWR and drives the mutations.
async function getInitialBoard(
  deliveryId: string,
  query: string
): Promise<InitialDeliveryBoard | null> {
  try {
    const authObj = await auth();
    if (!authObj?.userId || !authObj.orgId) return null;
    const token = await getClerkToken(authObj.getToken);
    if (!token) return null;
    const response = await fetch(
      getBackendUrl(
        "deliveries",
        `/${encodeURIComponent(deliveryId)}/view${query}`
      ),
      { cache: "no-store", headers: getAuthHeaders(token) }
    );
    if (!response.ok) {
      console.error(
        `[deliveries/[delivery]/page] Failed initial fetch: ${response.status}`
      );
      return null;
    }
    return {
      board: await response.json(),
      userId: authObj.userId,
      orgId: authObj.orgId,
      fetchedAt: Date.now(),
      query,
    };
  } catch (error) {
    console.error("[deliveries/[delivery]/page] Initial fetch failed", error);
    return null;
  }
}

export default async function DeliveryPage({
  params,
  searchParams,
}: {
  params: Promise<{ delivery: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { delivery } = await params;
  const search = await searchParams;
  const query = deliveryPageQuery({
    get: (key) => {
      const value = search[key];
      return (Array.isArray(value) ? value[0] : value) ?? null;
    },
  });
  const initialBoard = await getInitialBoard(delivery, query);
  return (
    <DeliveryBoardClient deliveryId={delivery} initialBoard={initialBoard} />
  );
}
