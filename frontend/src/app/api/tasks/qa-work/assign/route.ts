import { NextRequest } from "next/server";
import { proxyJsonRequest } from "@/lib/backend-response";

export async function POST(request: NextRequest) {
  return proxyJsonRequest(request, "tasks/qa-work/assign", "POST");
}
