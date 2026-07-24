import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import { decodeExperimentRouteParam } from "@/lib/utils";

type RouteParams = { params: Promise<{ experiment: string; task_id: string }> };

async function proxy(
  method: "PUT" | "DELETE",
  { params }: RouteParams,
  body?: string
) {
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
    return NextResponse.json({ error: "Missing experiment" }, { status: 400 });
  }

  const url = getBackendUrl(
    "experiments",
    `/${encodeURIComponent(experimentId)}/tasks/${encodeURIComponent(task_id)}/default-version`
  );
  const res = await fetch(url, {
    method,
    cache: "no-store",
    headers: body
      ? { ...getAuthHeaders(token), "Content-Type": "application/json" }
      : getAuthHeaders(token),
    body,
  });

  const data = await res.json().catch(() => ({ error: "Upstream error" }));
  return NextResponse.json(data, { status: res.status });
}

export async function PUT(request: NextRequest, ctx: RouteParams) {
  try {
    const payload = await request.json().catch(() => ({}));
    return await proxy("PUT", ctx, JSON.stringify(payload));
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 }
    );
  }
}

export async function DELETE(_request: NextRequest, ctx: RouteParams) {
  try {
    return await proxy("DELETE", ctx);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 }
    );
  }
}
