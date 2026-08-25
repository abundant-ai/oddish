import { proxyBackendResponse } from "@/lib/backend-response";
import { decodeExperimentRouteParam } from "@/lib/utils";

export async function GET(
  request: Request,
  { params }: { params: Promise<{ experiment: string }> },
) {
  const { experiment } = await params;
  const experimentId = decodeExperimentRouteParam(experiment ?? "");
  const query = new URL(request.url).search;
  return proxyBackendResponse({
    request,
    path: `experiments/${encodeURIComponent(experimentId)}/trial-page${query}`,
  });
}
