import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import { decodeExperimentRouteParam } from "@/lib/utils";
import {
  joinServerTimingHeaders,
  ServerTimingCollector,
} from "@/lib/server-timing";

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ experiment: string }> },
) {
  const timings = new ServerTimingCollector();
  const requestStartedAt = performance.now();

  try {
    const authObj = await timings.measureAsync(
      "next_auth",
      () => auth(),
      "Clerk auth",
    );

    if (!authObj || !authObj.userId) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const token = await timings.measureAsync(
      "next_token",
      () => getClerkToken(authObj.getToken),
      "Clerk token",
    );

    if (!token) {
      console.error("Failed to get Clerk token for user:", authObj.userId);
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

    const searchParams = request.nextUrl.searchParams;
    const queryParams = Object.fromEntries(searchParams.entries());
    if (queryParams.include_trials === "true" && !queryParams.compact_trials) {
      queryParams.compact_trials = "true";
    }
    const url = getBackendUrl("tasks", "", {
      ...queryParams,
      experiment_id: experimentId,
    });

    const res = await timings.measureAsync(
      "next_upstream",
      () =>
        fetch(url, {
          cache: "no-store",
          headers: getAuthHeaders(token),
        }),
      "Backend fetch",
    );

    if (!res.ok) {
      const errorText = await timings.measureAsync(
        "next_error_body",
        () => res.text(),
        "Read error body",
      );
      console.error(`Backend error: ${res.status} - ${errorText}`);
      return NextResponse.json(
        { error: "Failed to fetch experiment tasks", details: errorText },
        { status: res.status },
      );
    }

    const data = await timings.measureAsync(
      "next_json",
      () => res.json(),
      "Decode JSON",
    );
    timings.add(
      "next_total",
      performance.now() - requestStartedAt,
      "Experiment tasks proxy total",
    );
    const response = NextResponse.json(data);
    response.headers.set(
      "Cache-Control",
      "private, max-age=3, stale-while-revalidate=10",
    );
    const serverTiming = joinServerTimingHeaders(
      timings.toHeader(),
      res.headers.get("server-timing"),
    );
    if (serverTiming) {
      response.headers.set("Server-Timing", serverTiming);
    }
    return response;
  } catch (error) {
    console.error("API route error:", error);
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
