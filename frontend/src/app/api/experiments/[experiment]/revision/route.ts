import { NextRequest, NextResponse } from "next/server";
import { proxyBackendResponse } from "@/lib/backend-response";
import { decodeExperimentRouteParam } from "@/lib/utils";

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ experiment: string }> }
) {
  const experimentId = decodeExperimentRouteParam((await params).experiment);
  if (!experimentId) {
    return NextResponse.json({ error: "Missing experiment" }, { status: 400 });
  }
  return proxyBackendResponse({
    request,
    path: `experiments/${encodeURIComponent(experimentId)}/revision`,
  });
}
