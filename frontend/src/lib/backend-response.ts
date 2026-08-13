import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import { getAuthHeaders, getBackendUrl, getClerkToken } from "./backend-config";
import {
  attachUpstreamServerTiming,
  backendFetchHeaders,
} from "./proxy-headers";

type JsonObject = Record<string, unknown>;

type BackendJsonResult = {
  data: unknown;
  parseError: JsonObject | null;
  status: number;
};

export async function readBackendJson(
  response: Response,
  fallbackError: string
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
  fallbackError: string
): JsonObject {
  if (payload && typeof payload === "object" && !Array.isArray(payload)) {
    return payload as JsonObject;
  }

  if (typeof payload === "string" && payload.trim()) {
    return { error: payload.trim() };
  }

  return { error: fallbackError };
}

// `signal` propagates client aborts upstream: pass the route's
// request.signal so a disconnected caller cancels the backend call too,
// instead of leaving it running for a response nobody will read.
export async function proxyBackendJson({
  request,
  path,
  method = "GET",
  body,
  signal,
}: {
  request: Request;
  path: string;
  method?: "GET" | "PUT" | "POST" | "PATCH" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
}): Promise<NextResponse> {
  try {
    const { getToken } = await auth();
    const token = await getClerkToken(getToken);
    if (!token) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const sendsBody = body !== undefined;
    const res = await fetch(getBackendUrl(path), {
      method,
      cache: "no-store",
      signal,
      headers: backendFetchHeaders(
        request,
        sendsBody
          ? { "Content-Type": "application/json", ...getAuthHeaders(token) }
          : getAuthHeaders(token)
      ),
      body: sendsBody ? JSON.stringify(body) : undefined,
    });

    const { data, parseError, status } = await readBackendJson(
      res,
      "Upstream error"
    );
    if (parseError) {
      return attachUpstreamServerTiming(
        NextResponse.json(parseError, { status }),
        res
      );
    }
    if (!res.ok) {
      return attachUpstreamServerTiming(
        NextResponse.json(backendErrorPayload(data, "Upstream error"), {
          status: res.status,
        }),
        res
      );
    }
    return attachUpstreamServerTiming(
      data === null
        ? NextResponse.json({ error: "Upstream error" }, { status: 502 })
        : NextResponse.json(data),
      res
    );
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 }
    );
  }
}

export async function proxyJsonRequest(
  request: NextRequest,
  path: string,
  method: "PUT" | "POST" | "PATCH"
): Promise<NextResponse> {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
  }
  return proxyBackendJson({ request, path, method, body });
}
