import { proxyPublicBackendResponse } from "@/lib/backend-response";

export async function GET(
  request: Request,
  { params }: { params: Promise<{ token: string; trial_id: string }> }
) {
  const { token, trial_id } = await params;
  return proxyPublicBackendResponse({
    request,
    path: `public/experiments/${encodeURIComponent(token)}/trials/${encodeURIComponent(trial_id)}`,
  });
}
