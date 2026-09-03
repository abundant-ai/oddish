import { NextRequest } from "next/server";
import { proxyBackendJson, proxyJsonRequest } from "@/lib/backend-response";

const PATH = "deliveries";

export const GET = (request: NextRequest) =>
  proxyBackendJson({ request, path: PATH, signal: request.signal });

export const POST = (request: NextRequest) =>
  proxyJsonRequest(request, PATH, "POST");
