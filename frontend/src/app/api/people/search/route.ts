import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

const NO_STORE_HEADERS = { "Cache-Control": "no-store" };

export async function GET(request: NextRequest) {
  try {
    const authObj = await auth();
    if (!authObj?.userId) {
      return NextResponse.json(
        { error: "Unauthorized" },
        { status: 401, headers: NO_STORE_HEADERS }
      );
    }

    const token = await getClerkToken(authObj.getToken);
    if (!token) {
      return NextResponse.json(
        { error: "Failed to get authentication token" },
        { status: 401, headers: NO_STORE_HEADERS }
      );
    }

    const searchParams = request.nextUrl.searchParams;
    const response = await fetch(
      getBackendUrl("people/search", "", {
        q: searchParams.get("q") ?? "",
        limit: searchParams.get("limit") ?? "10",
      }),
      {
        cache: "no-store",
        headers: getAuthHeaders(token),
      }
    );

    return new NextResponse(await response.text(), {
      status: response.status,
      headers: {
        "Cache-Control": "no-store",
        "Content-Type":
          response.headers.get("content-type") ?? "application/json",
      },
    });
  } catch (error) {
    console.error("People search API route error:", error);
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503, headers: NO_STORE_HEADERS }
    );
  }
}
