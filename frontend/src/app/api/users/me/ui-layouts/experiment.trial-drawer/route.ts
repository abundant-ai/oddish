import { NextRequest } from "next/server";
import { proxyBackendJson, proxyJsonRequest } from "@/lib/backend-response";

const PATH = "users/me/ui-layouts/experiment.trial-drawer";

export function GET(request: NextRequest) {
  return proxyBackendJson({ request, path: PATH, signal: request.signal });
}

export function PUT(request: NextRequest) {
  return proxyJsonRequest(request, PATH, "PUT");
}
