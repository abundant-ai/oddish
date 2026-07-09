import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

// GET returns the STORED agent graph (or {status: "not_generated"}); it never
// runs the LLM. POST is the explicit trigger that generates + persists it.
export async function GET(
  _request: Request,
  { params }: { params: Promise<{ trial_id: string }> },
) {
  try {
    const { getToken } = await auth();
    const token = await getClerkToken(getToken);
    const { trial_id } = await params;

    const url = getBackendUrl("trials", `/${trial_id}/trajectory/graph`);
    const res = await fetch(url, {
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

export async function POST(
  request: Request,
  { params }: { params: Promise<{ trial_id: string }> },
) {
  try {
    const { getToken } = await auth();
    const token = await getClerkToken(getToken);
    const { trial_id } = await params;
    const refresh = new URL(request.url).searchParams.get("refresh");

    const suffix = refresh === "true" ? "?refresh=true" : "";
    const url = getBackendUrl(
      "trials",
      `/${trial_id}/trajectory/graph${suffix}`,
    );
    const res = await fetch(url, {
      method: "POST",
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
