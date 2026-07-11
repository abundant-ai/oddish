import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import { decodeExperimentRouteParam } from "@/lib/utils";

export async function DELETE(
  _request: Request,
  { params }: { params: Promise<{ experiment: string; task_id: string }> }
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
        { status: 401 }
      );
    }

    const { experiment, task_id } = await params;
    const experimentId = decodeExperimentRouteParam(experiment);
    if (!experimentId) {
      return NextResponse.json(
        { error: "Missing experiment" },
        { status: 400 }
      );
    }

    const url = getBackendUrl(
      "experiments",
      `/${encodeURIComponent(experimentId)}/tasks/${encodeURIComponent(task_id)}`
    );
    const res = await fetch(url, {
      method: "DELETE",
      cache: "no-store",
      headers: getAuthHeaders(token),
    });

    const text = await res.text();
    let data: unknown = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      if (!res.ok) {
        return NextResponse.json(
          { error: text || "Upstream error" },
          { status: res.status }
        );
      }
    }

    if (!res.ok) {
      return NextResponse.json(data ?? { error: "Upstream error" }, {
        status: res.status,
      });
    }

    return NextResponse.json(data);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 }
    );
  }
}
