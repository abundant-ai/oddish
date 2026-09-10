import { proxyBackendJson } from "@/lib/backend-response";

export async function POST(
  request: Request,
  context: { params: Promise<{ id: string }> }
) {
  const { id } = await context.params;
  return proxyBackendJson({
    request,
    path: `admin/endpoint-health/${encodeURIComponent(id)}/check`,
    method: "POST",
  });
}
