import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import { decodeExperimentRouteParam } from "@/lib/utils";

async function getToken() {
  const authObj = await auth();
  if (!authObj || !authObj.userId) {
    return { error: "Unauthorized", status: 401 } as const;
  }
  const token = await getClerkToken(authObj.getToken);
  if (!token) {
    return { error: "Failed to get authentication token", status: 401 } as const;
  }
  return { token, authObj } as const;
}

function requireAdmin(authObj: { orgRole?: string | null }) {
  if (!["org:admin", "org:owner"].includes(authObj.orgRole ?? "")) {
    return NextResponse.json(
      { error: "Forbidden: admin privileges required" },
      { status: 403 },
    );
  }
  return null;
}

export async function PATCH(
  request: Request,
  { params }: { params: Promise<{ experiment: string; cell: string }> },
) {
  try {
    const tk = await getToken();
    if ("error" in tk) {
      return NextResponse.json({ error: tk.error }, { status: tk.status });
    }
    const denied = requireAdmin(tk.authObj);
    if (denied) return denied;
    const { experiment, cell } = await params;
    const experimentId = decodeExperimentRouteParam(experiment);
    const url = getBackendUrl(
      "experiments",
      `/${encodeURIComponent(experimentId)}/cells/${encodeURIComponent(cell)}`,
    );
    const body = await request.text();
    const res = await fetch(url, {
      method: "PATCH",
      cache: "no-store",
      headers: {
        ...getAuthHeaders(tk.token),
        "content-type": "application/json",
      },
      body,
    });
    const text = await res.text();
    const data = text ? JSON.parse(text) : null;
    if (!res.ok) {
      return NextResponse.json(data ?? { error: "Upstream error" }, {
        status: res.status,
      });
    }
    return NextResponse.json(data);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}

