import { NextResponse } from "next/server";
import { getBackendUrl } from "@/lib/backend-config";

export async function GET() {
  try {
    const res = await fetch(getBackendUrl("health"), {
      cache: "no-store",
    });

    if (!res.ok) {
      return NextResponse.json(
        { status: "degraded", error: "Backend health check failed" },
        { status: 503 },
      );
    }

    const data = await res.json().catch(() => ({ status: "healthy" }));
    return NextResponse.json(data);
  } catch (error) {
    console.error("Health route error:", error);
    return NextResponse.json(
      { status: "degraded", error: "Health check unavailable" },
      { status: 503 },
    );
  }
}
