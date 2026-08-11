import { proxyBackendJson } from "@/lib/backend-response";

export async function GET(request: Request) {
  return proxyBackendJson({ request, path: "admin/operator-access" });
}
