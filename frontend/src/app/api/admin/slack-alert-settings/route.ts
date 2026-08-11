import { NextRequest } from "next/server";
import { proxyBackendJson, proxyJsonRequest } from "@/lib/backend-response";

const PATH = "admin/slack-alert-settings";

export async function GET(request: NextRequest) {
  return proxyBackendJson({ request, path: PATH });
}

export async function PUT(request: NextRequest) {
  return proxyJsonRequest(request, PATH, "PUT");
}

export async function DELETE(request: NextRequest) {
  return proxyBackendJson({ request, path: PATH, method: "DELETE" });
}
