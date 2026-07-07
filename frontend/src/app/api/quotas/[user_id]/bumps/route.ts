import { NextRequest } from "next/server";
import { proxyBackendJson, proxyJsonRequest } from "@/lib/backend-response";

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ user_id: string }> },
) {
  const { user_id } = await params;
  return proxyJsonRequest(
    request,
    `quotas/${encodeURIComponent(user_id)}/bumps`,
    "POST",
  );
}

export async function DELETE(
  _request: NextRequest,
  { params }: { params: Promise<{ user_id: string }> },
) {
  const { user_id } = await params;
  return proxyBackendJson({
    path: `quotas/${encodeURIComponent(user_id)}/bumps`,
    method: "DELETE",
  });
}
