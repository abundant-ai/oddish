import { NextRequest } from "next/server";
import { proxyBackendJson } from "@/lib/backend-response";

export async function GET(request: NextRequest) {
  return proxyBackendJson({ request, path: "customers" });
}
