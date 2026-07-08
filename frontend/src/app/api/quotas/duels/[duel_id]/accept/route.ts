import { NextRequest } from "next/server";
import { proxyBackendJson } from "@/lib/backend-response";

export async function POST(
  _request: NextRequest,
  { params }: { params: Promise<{ duel_id: string }> },
) {
  const { duel_id } = await params;
  return proxyBackendJson({
    path: `quotas/duels/${encodeURIComponent(duel_id)}/accept`,
    method: "POST",
    body: {},
  });
}
