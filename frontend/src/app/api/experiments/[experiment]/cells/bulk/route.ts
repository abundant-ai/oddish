import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import { decodeExperimentRouteParam } from "@/lib/utils";

export async function POST(
  request: Request,
  { params }: { params: Promise<{ experiment: string }> },
) {
  try {
    const authObj = await auth();
    if (!authObj || !authObj.userId) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }
    if (!["org:admin", "org:owner"].includes(authObj.orgRole ?? "")) {
      return NextResponse.json(
        { error: "Forbidden: admin privileges required" },
        { status: 403 },
      );
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
      `/${encodeURIComponent(experimentId)}/cells/bulk`,
    );
    const body = await request.text();
    const res = await fetch(url, {
      method: "POST",
      cache: "no-store",
      headers: {
        ...getAuthHeaders(token),
        "content-type": "application/json",
      },
      body,
    });
    const text = await res.text();
    let data: unknown = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = null;
    }
    if (!res.ok) {
      return NextResponse.json(
        data ?? { error: text || `Upstream ${res.status}` },
        { status: res.status },
      );
    }
    return NextResponse.json(data);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
