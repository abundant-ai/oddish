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
  { params }: { params: Promise<{ task_id: string }> },
) {
  try {
    const { getToken } = await auth();
    const token = await getClerkToken(getToken);

    const { task_id } = await params;

    // Every param must be forwarded explicitly: getBackendUrl builds a fresh
    // URL, so anything not named here is silently dropped. `refresh` would
    // return the cached comparison it asked to replace, and `version` would
    // compare the current task version while the overview shows an older one.
    const incoming = new URL(request.url).searchParams;
    const forwarded: Record<string, string> = {};
    for (const name of ["refresh", "version"]) {
      const value = incoming.get(name);
      if (value !== null) forwarded[name] = value;
    }
    const url = getBackendUrl(
      "tasks",
      `/${task_id}/cohort-comparison`,
      Object.keys(forwarded).length ? forwarded : undefined,
    );
    const res = await fetch(url, {
      cache: "no-store",
      headers: backendFetchHeaders(request, getAuthHeaders(token)),
    });

    if (!res.ok) {
      const error = await res.json().catch(() => ({ error: "Upstream error" }));
      return attachUpstreamServerTiming(
        NextResponse.json(error, { status: res.status }),
        res,
      );
    }

    const data = await res.json();
    return attachUpstreamServerTiming(NextResponse.json(data), res);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
