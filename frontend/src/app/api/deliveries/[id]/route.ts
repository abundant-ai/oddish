import { NextRequest } from "next/server";
import { proxyBackendJson } from "@/lib/backend-response";

type Params = { params: Promise<{ id: string }> };

export async function GET(request: NextRequest, { params }: Params) {
  const { id } = await params;
  return proxyBackendJson({
    request,
    path: `deliveries/${encodeURIComponent(id)}`,
    signal: request.signal,
  });
}
