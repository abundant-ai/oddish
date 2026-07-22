import { NextRequest } from "next/server";
import { proxyBackendJson, proxyJsonRequest } from "@/lib/backend-response";

const PATH = "users/me/alert-preferences";

export async function GET() {
  return proxyBackendJson({ path: PATH });
}

export async function PUT(request: NextRequest) {
  return proxyJsonRequest(request, PATH, "PUT");
}
