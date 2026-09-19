import { NextRequest } from "next/server";
import { proxyJsonRequest } from "@/lib/backend-response";

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ task_id: string }> }
) {
  const { task_id } = await params;
  return proxyJsonRequest(
    request,
    `tasks/${encodeURIComponent(task_id)}/feedback`,
    "POST"
  );
}
