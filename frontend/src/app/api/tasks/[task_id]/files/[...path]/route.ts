import { encodeFilePath } from "@/lib/file-path";
import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
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
  { params }: { params: Promise<{ task_id: string; path: string[] }> }
) {
  try {
    const { getToken } = await auth();
    const token = await getClerkToken(getToken);

    const { task_id, path } = await params;
    const filePath = encodeFilePath(path.join("/"));
    const search = request.nextUrl.search;

    const url = getBackendUrl(
      "tasks",
      `/${task_id}/files/${filePath}${search}`
    );
    const res = await fetch(url, {
      cache: "no-store",
      headers: backendFetchHeaders(request, getAuthHeaders(token)),
    });

    if (!res.ok) {
      const error = await res.json().catch(() => ({ detail: res.statusText }));
      return attachUpstreamServerTiming(
        NextResponse.json(error, { status: res.status }),
        res
      );
    }

    const data = await res.json();

    // Text may be cached; renewing a temporary URL must obtain a fresh signature.
    return attachUpstreamServerTiming(
      NextResponse.json(data, {
        headers: {
          "Cache-Control": data.url
            ? "private, no-store"
            : "private, max-age=300, stale-while-revalidate=60",
        },
      }),
      res
    );
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 }
    );
  }
}
