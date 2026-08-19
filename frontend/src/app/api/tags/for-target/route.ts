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

export async function GET(request: NextRequest) {
  try {
    const { getToken } = await auth();
    const token = await getClerkToken(getToken);
    if (!token) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const scope = request.nextUrl.searchParams.get("scope") ?? "";
    const targetId = request.nextUrl.searchParams.get("target_id") ?? "";
    const url = getBackendUrl("tags", "/for-target", {
      scope,
      target_id: targetId,
    });
    const res = await fetch(url, {
      cache: "no-store",
      headers: backendFetchHeaders(request, getAuthHeaders(token)),
    });

    const text = await res.text();
    let data: unknown = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = { error: text };
    }

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
