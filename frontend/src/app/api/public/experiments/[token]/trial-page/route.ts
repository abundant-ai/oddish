import { proxyPublicBackendResponse } from "@/lib/backend-response";

export async function GET(
  request: Request,
  { params }: { params: Promise<{ token: string }> },
) {
  const { token } = await params;
  const query = new URL(request.url).search;
  return proxyPublicBackendResponse({
    request,
    path: `public/experiments/${encodeURIComponent(token)}/trial-page${query}`,
  });
}
