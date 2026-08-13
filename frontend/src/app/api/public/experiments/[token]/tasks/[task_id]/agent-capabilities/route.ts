import { NextResponse } from "next/server";
import { getBackendUrl } from "@/lib/backend-config";

export async function GET(
  request: Request,
  { params }: { params: Promise<{ token: string; task_id: string }> },
) {
  try {
    const { token, task_id } = await params;
    // Forwarded, not dropped: the comparison is per task version, and a share
    // page pins the version its trials ran on. Without it the backend falls
    // back to the task's current version and the page shows a comparison of
    // runs it is not displaying.
    const version = new URL(request.url).searchParams.get("version");
    const url = getBackendUrl(
      "public/experiments",
      `/${token}/tasks/${task_id}/agent-capabilities${
        version ? `?version=${encodeURIComponent(version)}` : ""
      }`,
    );
    const res = await fetch(url, { cache: "no-store" });

    const text = await res.text();
    const data = text ? JSON.parse(text) : null;

    if (!res.ok) {
      return NextResponse.json(data ?? { error: "Upstream error" }, {
        status: res.status,
      });
    }

    return NextResponse.json(data, { status: res.status });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
