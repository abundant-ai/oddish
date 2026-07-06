import { proxyBackendJson } from "@/lib/backend-response";

export async function GET() {
  return proxyBackendJson({ path: "quotas" });
}
