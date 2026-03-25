import { NextRequest, NextResponse } from "next/server";
import { getBackendUrl } from "@/lib/backend-config";

export async function GET(request: NextRequest) {
  try {
    const searchParams = request.nextUrl.searchParams;
    const queryString = searchParams.toString();
    const baseUrl = getBackendUrl("public/experiments");
    const url = queryString ? `${baseUrl}?${queryString}` : baseUrl;

    const res = await fetch(url, { cache: "no-store" });
    const text = await res.text();
    let data: unknown = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = { error: text };
      }
    }

    if (!res.ok) {
      const upstream =
        typeof data === "object" && data !== null
          ? data
          : { error: "Upstream error" };
      return NextResponse.json(upstream, {
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
