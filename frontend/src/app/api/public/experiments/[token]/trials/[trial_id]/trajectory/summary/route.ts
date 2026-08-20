import { NextResponse } from "next/server";
import { getBackendUrl } from "@/lib/backend-config";

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ token: string; trial_id: string }> },
) {
  try {
    const { token, trial_id } = await params;
    const url = getBackendUrl(
      "public/experiments",
      `/${token}/trials/${trial_id}/trajectory/summary`,
    );
    const res = await fetch(url, { cache: "no-store" });

    const text = await res.text();
    const data = text ? JSON.parse(text) : null;

    if (!res.ok) {
      return NextResponse.json(data ?? { error: "Upstream error" }, {
        status: res.status,
      });
    }

    // Forward the upstream status even when ok (2xx isn't only 200), so the
    // polling hook's status-code contract survives this proxy too.
    return NextResponse.json(data, { status: res.status });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
