import { auth } from "@clerk/nextjs/server";
import { NextRequest, NextResponse } from "next/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import {
  attachUpstreamServerTiming,
  backendFetchHeaders,
} from "@/lib/proxy-headers";

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ task_id: string }> }
) {
  try {
    const { getToken } = await auth();
    const token = await getClerkToken(getToken);
    const { task_id } = await params;
    const versionId = request.nextUrl.searchParams.get("version_id");
    const upstream = await fetch(
      getBackendUrl(
        "tasks",
        `/${task_id}/open`,
        versionId ? { version_id: versionId } : undefined
      ),
      {
        cache: "no-store",
        headers: backendFetchHeaders(request, getAuthHeaders(token)),
      }
    );
    const response = new NextResponse(upstream.body, {
      status: upstream.status,
      headers: {
        "Cache-Control": "no-store",
        "Content-Type":
          upstream.headers.get("content-type") ?? "application/json",
      },
    });
    return attachUpstreamServerTiming(response, upstream);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503, headers: { "Cache-Control": "no-store" } }
    );
  }
}
