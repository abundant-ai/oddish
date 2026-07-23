import { NextRequest, NextResponse } from "next/server";
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

    const url = getBackendUrl("admin/cost-excluded-keys");
    const res = await fetch(url, {
      cache: "no-store",
      headers: getAuthHeaders(token),
    });

    if (!res.ok) {
      const errorText = await res.text();
      console.error(
        `[cost-excluded-keys] Backend error: ${res.status} - ${errorText}`
      );
      return NextResponse.json(
        { error: "Failed to fetch cost-excluded keys", details: errorText },
        { status: res.status }
      );
    }

    return NextResponse.json(await res.json());
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 }
    );
  }
}

export async function POST(request: NextRequest) {
  try {
    const { getToken } = await auth();
    const token = await getClerkToken(getToken);

    if (!token) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const body = await request.json();
    const url = getBackendUrl("admin/cost-excluded-keys");

    const res = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...getAuthHeaders(token),
      },
      body: JSON.stringify(body),
    });

    if (!res.ok) {
      const errorText = await res.text();
      console.error(
        `[cost-excluded-keys] Backend error: ${res.status} - ${errorText}`
      );
      return NextResponse.json(
        { error: "Failed to add cost-excluded key", details: errorText },
        { status: res.status }
      );
    }

    return NextResponse.json(await res.json());
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 }
    );
  }
}
