import { NextRequest } from "next/server";
import { proxyBackendJson, proxyJsonRequest } from "@/lib/backend-response";

type Params = { params: Promise<{ id: string }> };

export async function GET(request: NextRequest, { params }: Params) {
  const { id } = await params;
  return proxyBackendJson({
    request,
    path: `deliveries/${encodeURIComponent(id)}`,
    signal: request.signal,
  });
}

export async function PATCH(request: NextRequest, { params }: Params) {
  const { id } = await params;
  return proxyJsonRequest(
    request,
    `deliveries/${encodeURIComponent(id)}`,
    "PATCH"
  );
}

export async function DELETE(request: NextRequest, { params }: Params) {
  const { id } = await params;
  return proxyBackendJson({
    request,
    path: `deliveries/${encodeURIComponent(id)}`,
    method: "DELETE",
  });
}
