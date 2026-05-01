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

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ experiment: string }> },
) {
  try {
    const tk = await getToken();
    if ("error" in tk) {
      return NextResponse.json({ error: tk.error }, { status: tk.status });
    }
    const { experiment } = await params;
    const experimentId = decodeExperimentRouteParam(experiment);
    const url = getBackendUrl(
      "experiments",
      `/${encodeURIComponent(experimentId)}/cells`,
    );
    const res = await fetch(url, {
      cache: "no-store",
      headers: getAuthHeaders(tk.token),
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

export async function POST(
  request: Request,
  { params }: { params: Promise<{ experiment: string }> },
) {
  try {
    const tk = await getToken();
    if ("error" in tk) {
      return NextResponse.json({ error: tk.error }, { status: tk.status });
    }
    if (!["org:admin", "org:owner"].includes(tk.authObj.orgRole ?? "")) {
      return NextResponse.json(
        { error: "Forbidden: admin privileges required" },
        { status: 403 },
      );
    }
    const { experiment } = await params;
    const experimentId = decodeExperimentRouteParam(experiment);
    const url = getBackendUrl(
      "experiments",
      `/${encodeURIComponent(experimentId)}/cells`,
    );
    const body = await request.text();
    const res = await fetch(url, {
      method: "POST",
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
    return NextResponse.json(data, { status: res.status });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
