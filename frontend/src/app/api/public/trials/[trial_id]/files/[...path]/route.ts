import { NextResponse } from "next/server";
import { getBackendUrl } from "@/lib/backend-config";

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ trial_id: string; path: string[] }> },
) {
  try {
    const { trial_id, path } = await params;
    const filePath = path.join("/");
    const url = getBackendUrl(
      "public/trials",
      `/${trial_id}/files/${filePath}`,
    );

    const res = await fetch(url, { cache: "no-store" });

    if (!res.ok) {
      const text = await res.text();
      let payload: Record<string, unknown> = { detail: res.statusText };
      if (text) {
        try {
          payload = JSON.parse(text) as Record<string, unknown>;
        } catch {
          payload = { detail: text };
        }
      }
      return NextResponse.json(payload, { status: res.status });
    }

    const contentType =
      res.headers.get("content-type") || "application/octet-stream";
    return new Response(res.body, {
      status: 200,
      headers: {
        "Content-Type": contentType,
        "Cache-Control": "public, max-age=300, stale-while-revalidate=60",
      },
    });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
