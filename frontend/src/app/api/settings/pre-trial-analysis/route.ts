import { NextRequest } from "next/server";
import { proxyBackendJson } from "@/lib/backend-response";

export async function GET() {
  return proxyBackendJson({ path: "org/settings/pre-trial-analysis" });
}

export async function PUT(request: NextRequest) {
  return proxyBackendJson({
    path: "org/settings/pre-trial-analysis",
    method: "PUT",
    body: await request.json(),
  });
}
