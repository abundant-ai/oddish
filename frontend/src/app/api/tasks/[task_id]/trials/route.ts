import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

export async function GET(
  request: Request,
  { params }: { params: Promise<{ task_id: string }> },
) {
  try {
    const { getToken } = await auth();
    const token = await getClerkToken(getToken);

    const { task_id } = await params;

    const incoming = new URL(request.url).searchParams;
    const probe = incoming.get("probe");
    const version = incoming.get("version");
    const queryParams: Record<string, string> = {};
    if (probe !== null) queryParams.probe = probe;
    if (version !== null) queryParams.version = version;
    const url = getBackendUrl(
      "tasks",
      `/${task_id}/trials`,
      Object.keys(queryParams).length > 0 ? queryParams : undefined,
    );
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
