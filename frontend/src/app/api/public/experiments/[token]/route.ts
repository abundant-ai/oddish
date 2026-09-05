import { NextRequest } from "next/server";
import { proxyPublicBackendJson } from "@/lib/backend-response";

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ token: string }> }
) {
  const { token } = await params;
  return proxyPublicBackendJson({
    request,
    path: `public/experiments/${encodeURIComponent(token)}`,
  });
}
