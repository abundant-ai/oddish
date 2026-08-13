import { NextResponse } from "next/server";
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

// Full single-trial detail. The experiment grid loads only slim trials; the
// detail panel fetches the full trial here when a cell is clicked.
export async function GET(
  request: Request,
  { params }: { params: Promise<{ trial_id: string }> },
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

    const { trial_id } = await params;
    const url = getBackendUrl("trials", `/${trial_id}`);
    const res = await fetch(url, {
      cache: "no-store",
      headers: backendFetchHeaders(request, getAuthHeaders(token)),
    });

    if (!res.ok) {
      const text = await res.text();
      return attachUpstreamServerTiming(
        NextResponse.json(
          { error: "Failed to fetch trial", details: text },
          { status: res.status },
        ),
        res,
      );
    }

    return attachUpstreamServerTiming(NextResponse.json(await res.json()), res);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}

export async function DELETE(
  request: Request,
  { params }: { params: Promise<{ trial_id: string }> },
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

    const { trial_id } = await params;
    const url = getBackendUrl("trials", `/${trial_id}`);
    const res = await fetch(url, {
      method: "DELETE",
      cache: "no-store",
      headers: backendFetchHeaders(request, getAuthHeaders(token)),
    });

    const text = await res.text();
    let data: unknown = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      if (!res.ok) {
        return attachUpstreamServerTiming(
          NextResponse.json(
            { error: text || "Upstream error" },
            { status: res.status },
          ),
          res,
        );
      }
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
