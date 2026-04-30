import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import { getAuthHeaders, getClerkToken } from "@/lib/backend-config";
import { decodeExperimentRouteParam } from "@/lib/utils";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export async function DELETE(
  _request: Request,
  {
    params,
  }: {
    params: Promise<{ experiment: string; session_id: string }>;
  },
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

    const { experiment, session_id } = await params;
    const experimentId = decodeExperimentRouteParam(experiment);
    const url = `${API_URL}/api/experiments/${encodeURIComponent(
      experimentId,
    )}/cc-session/${encodeURIComponent(session_id)}`;

    const res = await fetch(url, {
      method: "DELETE",
      cache: "no-store",
      headers: getAuthHeaders(token),
    });

    if (!res.ok && res.status !== 204) {
      const text = await res.text();
      const data = text ? JSON.parse(text) : null;
      return NextResponse.json(data ?? { error: "Upstream error" }, {
        status: res.status,
      });
    }

    return new NextResponse(null, { status: 204 });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
