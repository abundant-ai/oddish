import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import {
  attachUpstreamCacheHeaders,
  attachUpstreamServerTiming,
  backendFetchHeaders,
  notModifiedResponse,
} from "@/lib/proxy-headers";

// A finished trial's trajectory is immutable, so the backend marks it
// cacheable and answers If-None-Match with 304. Both pass through here
// unchanged; the upstream fetch stays `no-store` because the browser is
// the cache meant to hold the body.
export async function GET(
  request: Request,
  { params }: { params: Promise<{ trial_id: string }> },
) {
  try {
    const { getToken } = await auth();
    const token = await getClerkToken(getToken);

    const { trial_id } = await params;

    const url = getBackendUrl("trials", `/${trial_id}/trajectory`);
    const res = await fetch(url, {
      cache: "no-store",
      headers: backendFetchHeaders(request, getAuthHeaders(token)),
    });

    if (res.status === 304) {
      return notModifiedResponse(res);
    }

    const text = await res.text();
    const data = text ? JSON.parse(text) : null;

    if (!res.ok) {
      return NextResponse.json(data ?? { error: "Upstream error" }, {
        status: res.status,
      });
    }

    // Return null as valid response if no trajectory exists
    return attachUpstreamServerTiming(
      attachUpstreamCacheHeaders(NextResponse.json(data), res),
      res,
    );
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
