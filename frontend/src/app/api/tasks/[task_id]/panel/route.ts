import { NextRequest } from "next/server";
import { proxyBackendJson } from "@/lib/backend-response";

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ task_id: string }> }
) {
  const { task_id } = await params;
  return proxyBackendJson({
    request,
    path: `tasks/${encodeURIComponent(task_id)}/panel${request.nextUrl.search}`,
    signal: request.signal,
    stream: true,
  });
}
