import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import { getAuthHeaders, getBackendUrl, getClerkToken } from "./backend-config";

type JsonObject = Record<string, unknown>;

type BackendJsonResult = {
  data: unknown;
  parseError: JsonObject | null;
  status: number;
};

export async function readBackendJson(
  response: Response,
  fallbackError: string,
): Promise<BackendJsonResult> {
  const text = await response.text();
  const trimmed = text.trim();

  if (!trimmed) {
    return { data: null, parseError: null, status: response.status };
  }

  try {
    return {
      data: JSON.parse(trimmed) as unknown,
      parseError: null,
      status: response.status,
    };
  } catch {
    const snippet =
      trimmed.length > 200 ? `${trimmed.slice(0, 200)}...` : trimmed;
    return {
      data: null,
      parseError: {
        error: `Backend ${response.status}: ${snippet || fallbackError}`,
      },
      status: response.status >= 400 ? response.status : 502,
    };
  }
}

export function backendErrorPayload(
  payload: unknown,
  fallbackError: string,
): JsonObject {
  if (payload && typeof payload === "object" && !Array.isArray(payload)) {
    return payload as JsonObject;
  }

  if (typeof payload === "string" && payload.trim()) {
    return { error: payload.trim() };
  }

  return { error: fallbackError };
}

export async function proxyBackendJson({
  path,
  method = "GET",
  body,
}: {
  path: string;
  method?: "GET" | "PUT";
  body?: unknown;
}): Promise<NextResponse> {
  try {
    const { getToken } = await auth();
    const token = await getClerkToken(getToken);
    if (!token) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const res = await fetch(getBackendUrl(path), {
      method,
      cache: "no-store",
      headers:
        method === "PUT"
          ? { "Content-Type": "application/json", ...getAuthHeaders(token) }
          : getAuthHeaders(token),
      body: method === "PUT" ? JSON.stringify(body) : undefined,
    });

    const text = await res.text();
    let data: unknown = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        return NextResponse.json(
          { error: "Upstream error" },
          { status: res.ok ? 502 : res.status },
        );
      }
    }

    if (!res.ok) {
      return NextResponse.json(data ?? { error: "Upstream error" }, {
        status: res.status,
      });
    }
    // A 2xx with no JSON body is not a success the client can use.
    return data === null
      ? NextResponse.json({ error: "Upstream error" }, { status: 502 })
      : NextResponse.json(data);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
