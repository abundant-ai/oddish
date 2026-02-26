import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import { decodeExperimentRouteParam } from "@/lib/utils";

export async function PATCH(
  request: Request,
  { params }: { params: Promise<{ experiment: string }> },
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

    const body = await request.json().catch(() => null);
    if (!body || typeof body.name !== "string") {
      return NextResponse.json(
        { error: "Missing experiment name" },
        { status: 400 },
      );
    }

    const { experiment } = await params;
    const experimentId = decodeExperimentRouteParam(experiment);
    const url = getBackendUrl(
      "experiments",
      `/${encodeURIComponent(experimentId)}`,
    );

    const res = await fetch(url, {
      method: "PATCH",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        ...getAuthHeaders(token),
      },
      body: JSON.stringify({ name: body.name }),
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

export async function DELETE(
  _request: Request,
  { params }: { params: Promise<{ experiment: string }> },
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

    const { experiment } = await params;
    const experimentId = decodeExperimentRouteParam(experiment);
    const url = getBackendUrl(
      "experiments",
      `/${encodeURIComponent(experimentId)}`,
    );

    const res = await fetch(url, {
      method: "DELETE",
      cache: "no-store",
      headers: getAuthHeaders(token),
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
