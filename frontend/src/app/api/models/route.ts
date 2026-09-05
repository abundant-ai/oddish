import { NextRequest } from "next/server";
import { proxyBackendJson, proxyJsonRequest } from "@/lib/backend-response";

export async function GET(request: NextRequest) {
  return proxyBackendJson({ request, path: "models" });
}

export async function POST(request: NextRequest) {
  return proxyJsonRequest(request, "models/check", "POST");
}
