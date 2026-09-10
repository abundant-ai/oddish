"use client";
import { Suspense } from "react";
import { DeliveryBoardClient } from "../../../src/app/(app)/deliveries/[delivery]/delivery-board-client";
import { Providers } from "../../../src/app/providers";
export default function Page() {
  return (
    <Providers>
      <Suspense>
        <DeliveryBoardClient deliveryId="refresh-test" initialBoard={null} />
      </Suspense>
    </Providers>
  );
}
