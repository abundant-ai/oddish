import { NextRequest } from "next/server";
import { proxyBackendJson, proxyJsonRequest } from "@/lib/backend-response";

const PATH = "admin/slack-alert-settings";

export async function GET() {
  return proxyBackendJson({ path: PATH });
}

export async function PUT(request: NextRequest) {
  return proxyJsonRequest(request, PATH, "PUT");
}

export async function DELETE() {
  return proxyBackendJson({ path: PATH, method: "DELETE" });
}
