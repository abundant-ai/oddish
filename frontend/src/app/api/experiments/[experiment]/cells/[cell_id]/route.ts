import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import { decodeExperimentRouteParam } from "@/lib/utils";

async function getTokenOrResponse(): Promise<
  | { token: string; response?: never }
  | { token?: never; response: NextResponse }
> {
  const authObj = await auth();
  if (!authObj?.userId) {
    return {
      response: NextResponse.json({ error: "Unauthorized" }, { status: 401 }),
    };
  }
  const token = await getClerkToken(authObj.getToken);
  if (!token) {
    return {
      response: NextResponse.json(
        { error: "Failed to get authentication token" },
        { status: 401 }
      ),
    };
  }
  return { token };
}

export async function PATCH(
  request: Request,
  { params }: { params: Promise<{ experiment: string; cell_id: string }> }
) {
  try {
    const authResult = await getTokenOrResponse();
    if (authResult.response) return authResult.response;

    const { experiment, cell_id } = await params;
    const experimentId = decodeExperimentRouteParam(experiment);
    const body = await request.json();
    const res = await fetch(
      getBackendUrl(
        "experiments",
        `/${encodeURIComponent(experimentId)}/cells/${encodeURIComponent(cell_id)}`
      ),
      {
        method: "PATCH",
        cache: "no-store",
        headers: {
          "Content-Type": "application/json",
          ...getAuthHeaders(authResult.token),
        },
        body: JSON.stringify(body),
      }
    );
    const text = await res.text();
    const data = text ? JSON.parse(text) : null;
    if (!res.ok) {
      return NextResponse.json(data ?? { error: "Upstream error" }, {
        status: res.status,
      });
    }
    return NextResponse.json(data);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 }
    );
  }
}

export async function DELETE(
  _request: Request,
  { params }: { params: Promise<{ experiment: string; cell_id: string }> }
) {
  try {
    const authResult = await getTokenOrResponse();
    if (authResult.response) return authResult.response;

    const { experiment, cell_id } = await params;
    const experimentId = decodeExperimentRouteParam(experiment);
    const res = await fetch(
      getBackendUrl(
        "experiments",
        `/${encodeURIComponent(experimentId)}/cells/${encodeURIComponent(cell_id)}`
      ),
      {
        method: "DELETE",
        cache: "no-store",
        headers: getAuthHeaders(authResult.token),
      }
    );
    const text = await res.text();
    const data = text ? JSON.parse(text) : null;
    if (!res.ok) {
      return NextResponse.json(data ?? { error: "Upstream error" }, {
        status: res.status,
      });
    }
    return NextResponse.json(data);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 }
    );
  }
}
