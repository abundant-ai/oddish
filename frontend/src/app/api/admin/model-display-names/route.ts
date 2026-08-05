import { NextRequest } from "next/server";
import { proxyBackendJson, proxyJsonRequest } from "@/lib/backend-response";

const PATH = "admin/model-display-names";

export const GET = () => proxyBackendJson({ path: PATH });

export const POST = (request: NextRequest) =>
  proxyJsonRequest(request, PATH, "POST");
