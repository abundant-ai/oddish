import { proxyPublicBackendResponse } from "@/lib/backend-response";

export async function GET(
  request: Request,
  { params }: { params: Promise<{ token: string }> },
) {
  const { token } = await params;
  return proxyPublicBackendResponse({
    request,
    path: `public/experiments/${encodeURIComponent(token)}/revision`,
  });
}
