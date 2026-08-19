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
    const url = getBackendUrl("trials", `/${trial_id}/live${search}`);
    const res = await fetch(url, {
      cache: "no-store",
      headers: backendFetchHeaders(request, getAuthHeaders(token)),
    });

    const text = await res.text();
    const data = text ? JSON.parse(text) : null;

    if (!res.ok) {
      return attachUpstreamServerTiming(
        NextResponse.json(data ?? { error: "Upstream error" }, {
          status: res.status,
        }),
        res,
      );
    }

    return attachUpstreamServerTiming(NextResponse.json(data), res);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
