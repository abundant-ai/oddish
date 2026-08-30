import { NextRequest } from "next/server";
import { proxyBackendJson, proxyJsonRequest } from "@/lib/backend-response";

type Params = { params: Promise<{ id: string; taskId: string }> };

export async function PATCH(request: NextRequest, { params }: Params) {
  const { id, taskId } = await params;
  return proxyJsonRequest(
    request,
    `deliveries/${encodeURIComponent(id)}/tasks/${encodeURIComponent(taskId)}`,
    "PATCH"
  );
}

export async function DELETE(request: NextRequest, { params }: Params) {
  const { id, taskId } = await params;
  return proxyBackendJson({
    request,
    path: `deliveries/${encodeURIComponent(id)}/tasks/${encodeURIComponent(taskId)}`,
    method: "DELETE",
  });
}
