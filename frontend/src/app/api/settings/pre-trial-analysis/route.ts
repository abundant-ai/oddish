import { NextRequest } from "next/server";
import { proxyBackendJson } from "@/lib/backend-response";

export async function GET(request: NextRequest) {
  return proxyBackendJson({
    request,
    path: "org/settings/pre-trial-analysis",
  });
}

export async function PUT(request: NextRequest) {
  return proxyBackendJson({
    request,
    path: "org/settings/pre-trial-analysis",
    method: "PUT",
    body: await request.json(),
  });
}
