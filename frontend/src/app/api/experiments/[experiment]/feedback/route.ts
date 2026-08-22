import { NextRequest } from "next/server";
import { proxyJsonRequest } from "@/lib/backend-response";
import { decodeExperimentRouteParam } from "@/lib/utils";

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ experiment: string }> }
) {
  const { experiment } = await params;
  const experimentId = decodeExperimentRouteParam(experiment);
  return proxyJsonRequest(
    request,
    `experiments/${encodeURIComponent(experimentId)}/feedback`,
    "POST"
  );
}
