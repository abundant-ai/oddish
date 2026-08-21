import { NextRequest } from "next/server";
import { proxyBackendJson } from "@/lib/backend-response";

const PATH = "admin/cost-excluded-keys";

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  return proxyBackendJson({
    request,
    path: `${PATH}/${encodeURIComponent(id)}`,
    method: "DELETE",
  });
}
