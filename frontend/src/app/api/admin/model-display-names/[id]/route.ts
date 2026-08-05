import { NextRequest } from "next/server";
import { proxyBackendJson, proxyJsonRequest } from "@/lib/backend-response";

const PATH = "admin/model-display-names";

export async function PUT(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const encoded = encodeURIComponent(id);
  return proxyJsonRequest(request, `${PATH}/${encoded}`, "PUT");
}

export async function DELETE(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const encoded = encodeURIComponent(id);
  return proxyBackendJson({ path: `${PATH}/${encoded}`, method: "DELETE" });
}
