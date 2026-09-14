import { NextRequest } from "next/server";
import { proxyBackendJson } from "@/lib/backend-response";

type Params = { params: Promise<{ task_id: string }> };

export async function GET(request: NextRequest, { params }: Params) {
  const { task_id } = await params;
  return proxyBackendJson({
    request,
    path: `tasks/${encodeURIComponent(task_id)}/qa-history`,
    signal: request.signal,
  });
}
