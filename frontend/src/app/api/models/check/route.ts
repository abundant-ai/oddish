import { NextRequest } from "next/server";
import { proxyJsonRequest } from "@/lib/backend-response";

export async function POST(request: NextRequest) {
  return proxyJsonRequest(request, "models/check", "POST");
}
