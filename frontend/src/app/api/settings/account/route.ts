import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

export async function DELETE() {
  try {
    const { getToken } = await auth();
    const token = await getClerkToken(getToken);

    if (!token) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const url = getBackendUrl("users", "/me");

    const res = await fetch(url, {
      method: "DELETE",
      headers: getAuthHeaders(token),
    });

    if (!res.ok) {
      const errorText = await res.text();
      console.error(`[account] Backend error: ${res.status} - ${errorText}`);
      // Surface the backend's `detail` (e.g. the last-admin rejection) so the
      // dialog shows an actionable reason instead of a generic failure.
      let message = "Failed to delete account";
      try {
        const parsed = JSON.parse(errorText);
        if (typeof parsed?.detail === "string" && parsed.detail) {
          message = parsed.detail;
        }
      } catch {
        // Non-JSON backend error; keep the generic message.
      }
      return NextResponse.json(
        { error: message, details: errorText },
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
