import { auth } from "@clerk/nextjs/server";
import { NextRequest, NextResponse } from "next/server";

import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import {
  attachUpstreamServerTiming,
  backendFetchHeaders,
} from "@/lib/proxy-headers";

const SINGLE_VALUE_PARAMS = [
  "version",
  "experiment_id",
  "finding_limit",
  "finding_cursor",
  "trial_limit",
  "trial_cursor",
] as const;

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ task_id: string }> },
) {
  try {
    const { getToken } = await auth();
    const token = await getClerkToken(getToken);
    const { task_id } = await params;
    const upstreamUrl = new URL(getBackendUrl("tasks", `/${task_id}/review`));

    for (const key of SINGLE_VALUE_PARAMS) {
      const value = request.nextUrl.searchParams.get(key);
      if (value !== null) upstreamUrl.searchParams.set(key, value);
    }
    for (const tier of request.nextUrl.searchParams.getAll("tier")) {
      upstreamUrl.searchParams.append("tier", tier);
    }

    const upstream = await fetch(upstreamUrl, {
      cache: "no-store",
      headers: backendFetchHeaders(request, getAuthHeaders(token)),
    });
    const response = new NextResponse(upstream.body, {
      status: upstream.status,
      headers: {
        "Cache-Control": "no-store",
        "Content-Type":
          upstream.headers.get("content-type") ?? "application/json",
      },
    });
    return attachUpstreamServerTiming(response, upstream);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503, headers: { "Cache-Control": "no-store" } },
    );
  }
}
