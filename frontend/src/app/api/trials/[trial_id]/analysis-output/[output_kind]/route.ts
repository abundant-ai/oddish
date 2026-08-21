import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";

import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

export async function GET(
  _request: Request,
  {
    params,
  }: {
    params: Promise<{ trial_id: string; output_kind: string }>;
  }
) {
  try {
    const { getToken } = await auth();
    const token = await getClerkToken(getToken);
    const { trial_id, output_kind } = await params;
    const url = getBackendUrl(
      "trials",
      `/${trial_id}/analysis-output/${output_kind}`
    );
    const response = await fetch(url, {
      cache: "no-store",
      headers: getAuthHeaders(token),
    });
    if (!response.ok) {
      const text = await response.text();
      let payload: unknown = { detail: response.statusText };
      if (text) {
        try {
          payload = JSON.parse(text);
        } catch {
          payload = { detail: text };
        }
      }
      return NextResponse.json(payload, { status: response.status });
    }
    return new Response(response.body, {
      status: 200,
      headers: {
        "Content-Type":
          response.headers.get("content-type") ?? "application/octet-stream",
        "Cache-Control": "private, max-age=300",
      },
    });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 }
    );
  }
}
