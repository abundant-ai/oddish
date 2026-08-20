import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import {
  attachUpstreamServerTiming,
  backendFetchHeaders,
} from "@/lib/proxy-headers";

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ trial_id: string }> },
) {
  try {
    const { getToken } = await auth();
    const token = await getClerkToken(getToken);

    const { trial_id } = await params;

    const search = request.nextUrl.search;
    const url = getBackendUrl("trials", `/${trial_id}/files${search}`);
    const res = await fetch(url, {
      headers: backendFetchHeaders(request, getAuthHeaders(token)),
    });

    if (!res.ok) {
      const error = await res.json().catch(() => ({ detail: res.statusText }));
      return attachUpstreamServerTiming(
        NextResponse.json(error, { status: res.status }),
        res,
      );
    }

    const data = await res.json();
    return attachUpstreamServerTiming(
      NextResponse.json(data, {
        headers: {
          "Cache-Control": "private, max-age=600, stale-while-revalidate=60",
        },
      }),
      res,
    );
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
