import { pageFixture } from "../../delivery-page-fixtures";
import { Suspense } from "react";
import { DeliveryBoardClient } from "../../../src/app/(app)/deliveries/[delivery]/delivery-board-client";
import { Providers } from "../../../src/app/providers";
import { deliveryPageQuery } from "../../../src/lib/deliveries";
import { board } from "../../delivery-fixtures";

export default async function Page({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | undefined>>;
}) {
  const search = await searchParams;
  const { seed } = search;
  const params = new URLSearchParams(
    Object.entries(search).filter(
      (entry): entry is [string, string] => entry[1] !== undefined
    )
  );
  const query = deliveryPageQuery(params);
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
                  board: pageFixture(initial, params),
                  query,
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
