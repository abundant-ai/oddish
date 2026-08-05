import { NextRequest } from "next/server";
import { proxyBackendJson, proxyJsonRequest } from "@/lib/backend-response";

const PATH = "admin/model-display-names";

export async function GET() {
  return proxyBackendJson({ path: PATH });
}

export async function POST(request: NextRequest) {
  return proxyJsonRequest(request, PATH, "POST");
}
