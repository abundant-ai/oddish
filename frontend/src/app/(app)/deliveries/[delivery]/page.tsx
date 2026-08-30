import { DeliveryBoardClient } from "./delivery-board-client";

export default async function DeliveryPage({
  params,
}: {
  params: Promise<{ delivery: string }>;
}) {
  const { delivery } = await params;
  return <DeliveryBoardClient deliveryId={delivery} />;
}
