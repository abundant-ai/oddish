import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

export async function PUT(
  request: NextRequest,
  { params }: { params: Promise<{ vendor: string }> },
) {
  try {
    const { getToken } = await auth();
    const token = await getClerkToken(getToken);

    if (!token) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const { vendor } = await params;
    const body = await request.json();
    const url = getBackendUrl("byok", `/keys/${vendor}`);

    const res = await fetch(url, {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
        ...getAuthHeaders(token),
      },
      body: JSON.stringify(body),
    });

    if (!res.ok) {
      const errorText = await res.text();
      console.error(`[byok] Backend error: ${res.status} - ${errorText}`);
      return NextResponse.json(
        { error: "Failed to save BYOK key", details: errorText },
        { status: res.status },
      );
    }

    const data = await res.json();
    return NextResponse.json(data);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ vendor: string }> },
) {
  try {
    const { getToken } = await auth();
    const token = await getClerkToken(getToken);

    if (!token) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const { vendor } = await params;
    const url = getBackendUrl("byok", `/keys/${vendor}`);

    const res = await fetch(url, {
      method: "DELETE",
      headers: getAuthHeaders(token),
    });

    if (!res.ok) {
      const errorText = await res.text();
      console.error(`[byok] Backend error: ${res.status} - ${errorText}`);
      return NextResponse.json(
        { error: "Failed to delete BYOK key", details: errorText },
        { status: res.status },
      );
    }

    const data = await res.json();
    return NextResponse.json(data);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
