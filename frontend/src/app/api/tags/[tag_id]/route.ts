import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ tag_id: string }> },
) {
  try {
    const { getToken } = await auth();
    const token = await getClerkToken(getToken);
    if (!token) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const { tag_id } = await params;
    // Backend delete requires expected_row_version for optimistic concurrency.
    const body = await request.json();
    const url = getBackendUrl("tags", `/${encodeURIComponent(tag_id)}`);
    const res = await fetch(url, {
      method: "DELETE",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        ...getAuthHeaders(token),
      },
      body: JSON.stringify(body),
    });

    const text = await res.text();
    let data: unknown = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = { error: text };
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

export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ tag_id: string }> },
) {
  try {
    const { getToken } = await auth();
    const token = await getClerkToken(getToken);
    if (!token) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const { tag_id } = await params;
    const body = await request.json();
    const url = getBackendUrl("tags", `/${encodeURIComponent(tag_id)}`);
    const res = await fetch(url, {
      method: "PATCH",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        ...getAuthHeaders(token),
      },
      body: JSON.stringify(body),
    });

    const text = await res.text();
    let data: unknown = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = { error: text };
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
