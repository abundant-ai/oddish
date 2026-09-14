import { NextRequest } from "next/server";
import { proxyBackendJson } from "@/lib/backend-response";
import { decodeExperimentRouteParam } from "@/lib/utils";

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ experiment: string }> }
) {
  const { experiment } = await params;
  const id = encodeURIComponent(decodeExperimentRouteParam(experiment));
  const query = request.nextUrl.searchParams.toString();
  return proxyBackendJson({
    request,
    path: `experiments/${id}/trial-page${query ? `?${query}` : ""}`,
    signal: request.signal,
  });
}
