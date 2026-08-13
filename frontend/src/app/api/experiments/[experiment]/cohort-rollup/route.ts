import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import { decodeExperimentRouteParam } from "@/lib/utils";

// Mirrors the other `/api/experiments/[experiment]/...` proxies (see
// `cost-totals`, `probes`) rather than the `tasks/[task_id]/cohort-comparison`
// one: this route lives in that family, whose links are built with
// `encodeExperimentRouteParam` and so need the matching decode on the way in.
// No query params -- the backend route takes none (no `refresh`, no
// `version`; see its docstring for why).
export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ experiment: string }> },
) {
  try {
    const authObj = await auth();
    if (!authObj || !authObj.userId) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const token = await getClerkToken(authObj.getToken);
    if (!token) {
      return NextResponse.json(
        { error: "Failed to get authentication token" },
        { status: 401 },
      );
    }

    const { experiment } = await params;
    const experimentId = experiment
      ? decodeExperimentRouteParam(experiment)
      : "";
    if (!experimentId) {
      return NextResponse.json(
        { error: "Missing experiment" },
        { status: 400 },
      );
    }

    const url = getBackendUrl(
      "experiments",
      `/${encodeURIComponent(experimentId)}/cohort-rollup`,
    );

    const res = await fetch(url, {
      cache: "no-store",
      headers: getAuthHeaders(token),
    });

    const text = await res.text();
    const data = text ? JSON.parse(text) : null;

    if (!res.ok) {
      return NextResponse.json(data ?? { error: "Upstream error" }, {
        status: res.status,
      });
    }

    return NextResponse.json(data);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
