import { NextRequest } from "next/server";
import { proxyJsonRequest } from "@/lib/backend-response";

type Params = { params: Promise<{ id: string }> };

export async function PUT(request: NextRequest, { params }: Params) {
  const { id } = await params;
  return proxyJsonRequest(
    request,
    `deliveries/${encodeURIComponent(id)}/checks`,
    "PUT"
  );
}
