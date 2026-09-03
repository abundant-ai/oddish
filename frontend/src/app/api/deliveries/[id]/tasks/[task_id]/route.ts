import { NextRequest } from "next/server";
import { proxyBackendJson } from "@/lib/backend-response";

type Params = { params: Promise<{ id: string; task_id: string }> };

export async function DELETE(request: NextRequest, { params }: Params) {
  const { id, task_id } = await params;
  return proxyBackendJson({
    request,
    path: `deliveries/${encodeURIComponent(id)}/tasks/${encodeURIComponent(task_id)}`,
    method: "DELETE",
  });
}
