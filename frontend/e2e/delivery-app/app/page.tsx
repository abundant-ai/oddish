import { Suspense } from "react";
import { DeliveryBoardClient } from "../../../src/app/(app)/deliveries/[delivery]/delivery-board-client";
import { Providers } from "../../../src/app/providers";
import { board } from "../../delivery-fixtures";

export default async function Page({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | undefined>>;
}) {
  const { seed } = await searchParams;
  const initial = board();
  if (seed === "frozen") initial.frozen = true;
  if (seed === "wrong-org")
    initial.tasks[0].task_name = "OTHER_ORG_PRIVATE_TASK";
  return (
    <Providers>
      <Suspense>
        <DeliveryBoardClient
          deliveryId="refresh-test"
          initialBoard={
            seed
              ? {
                  board: initial,
                  userId: "user-1",
                  orgId: seed === "wrong-org" ? "org-2" : "org-1",
                  fetchedAt: Date.now() - (seed === "stale" ? 60000 : 0),
                }
              : null
          }
        />
      </Suspense>
    </Providers>
  );
}
