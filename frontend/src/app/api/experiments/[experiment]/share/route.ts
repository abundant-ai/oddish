import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import { decodeExperimentRouteParam } from "@/lib/utils";

export async function GET(
  _request: Request,
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
    const experimentId = decodeExperimentRouteParam(experiment);
    const url = getBackendUrl(
      "experiments",
      `/${encodeURIComponent(experimentId)}/share`,
    );

    const res = await fetch(url, {
      cache: "no-store",
      headers: getAuthHeaders(token),
      // Without a bound this proxy waits as long as the backend does
      // (10s+ observed at the tail). 12s covers the slowest backend
      // response seen in production while still answering before the
      // platform's function timeout would kill the route uncleanly.
      signal: AbortSignal.timeout(12_000),
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
    if (error instanceof Error && error.name === "TimeoutError") {
      return NextResponse.json(
        { error: "Backend timed out" },
        { status: 504 },
      );
    }
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
