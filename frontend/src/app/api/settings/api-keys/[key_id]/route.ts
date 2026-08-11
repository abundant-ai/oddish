import { NextRequest } from "next/server";
import { proxyBackendJson, proxyJsonRequest } from "@/lib/backend-response";

type RouteContext = { params: Promise<{ key_id: string }> };

export async function PATCH(request: NextRequest, { params }: RouteContext) {
  const { key_id } = await params;
  return proxyJsonRequest(request, `api-keys/${key_id}`, "PATCH");
}

export async function DELETE(request: NextRequest, { params }: RouteContext) {
  const { key_id } = await params;
  return proxyBackendJson({
    request,
    path: `api-keys/${key_id}`,
    method: "DELETE",
  });
}
