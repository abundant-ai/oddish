import { NextRequest } from "next/server";
import { proxyBackendJson, proxyJsonRequest } from "@/lib/backend-response";

const PATH = "users/me/alert-preferences";

export async function GET(request: NextRequest) {
  return proxyBackendJson({ request, path: PATH });
}

export async function PUT(request: NextRequest) {
  return proxyJsonRequest(request, PATH, "PUT");
}
