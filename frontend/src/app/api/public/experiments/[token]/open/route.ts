import { NextRequest } from "next/server";
import { proxyPublicBackendResponse } from "@/lib/backend-response";

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ token: string }> }
) {
  const { token } = await params;
  return proxyPublicBackendResponse({
    request,
    path: `public/experiments/${encodeURIComponent(token)}/open${request.nextUrl.search}`,
  });
}
