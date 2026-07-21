import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ trial_id: string }> },
) {
  try {
    const { getToken } = await auth();
    const token = await getClerkToken(getToken);

    const { trial_id } = await params;

    const url = getBackendUrl("trials", `/${trial_id}/trajectory/summary`);
    const res = await fetch(url, {
      cache: "no-store",
      headers: getAuthHeaders(token),
    });

    const text = await res.text();

    // Parse defensively: a crashing backend answers with a plain-text body
    // ("Internal Server Error"), and letting JSON.parse throw here would jump
    // to the catch below, discarding the real status and reporting the parse
    // error instead of the upstream failure.
    let data: unknown = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      return NextResponse.json(
        {
          error: `Backend returned ${res.status} ${res.statusText}`,
          detail: text.slice(0, 500),
        },
        { status: res.ok ? 502 : res.status },
      );
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
      { status: 503 },
    );
  }
}
