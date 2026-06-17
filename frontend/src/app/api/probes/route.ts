import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

export async function GET() {
  try {
    const { getToken } = await auth();
    const token = await getClerkToken(getToken);
    if (!token) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const url = getBackendUrl("probes");
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
