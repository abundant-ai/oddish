import { NextRequest } from "next/server";
import { proxyJsonRequest } from "@/lib/backend-response";

export async function PUT(
  request: NextRequest,
  { params }: { params: Promise<{ user_id: string }> },
) {
  const { user_id } = await params;
  return proxyJsonRequest(
    request,
    `quotas/${encodeURIComponent(user_id)}`,
    "PUT",
  );
}
