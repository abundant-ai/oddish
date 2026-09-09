import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import { getAuthHeaders, getBackendUrl, getClerkToken } from "./backend-config";
import {
  attachUpstreamServerTiming,
  backendFetchHeaders,
} from "./proxy-headers";

import {
  ServerTimingCollector,
  joinServerTimingHeaders,
} from "./server-timing";

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
  stream = false,
}: {
  request: Request;
  path: string;
  method?: "GET" | "PUT" | "POST" | "PATCH" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
  stream?: boolean;
}): Promise<NextResponse> {
  const timings = new ServerTimingCollector();
  const started = performance.now();
  let response: NextResponse;
  let upstream: Response | undefined;
  try {
    const { getToken } = await timings.measureAsync("next_auth", () => auth());
    const token = await timings.measureAsync("next_token", () =>
      getClerkToken(getToken)
    );
    if (!token) {
      response = NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    } else {
      const sendsBody = body !== undefined;
      const res = await timings.measureAsync("next_upstream", () =>
        fetch(getBackendUrl(path), {
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
        })
      );
      upstream = res;
      if (stream) {
        response = new NextResponse(res.body, {
          status: res.status,
          headers: {
            "Content-Type":
              res.headers.get("content-type") ?? "application/json",
          },
        });
      } else {
        const { data, parseError, status } = await timings.measureAsync(
          "next_json",
          () => readBackendJson(res, "Upstream error")
        );
        response = parseError
          ? NextResponse.json(parseError, { status })
          : !res.ok
            ? NextResponse.json(backendErrorPayload(data, "Upstream error"), {
                status: res.status,
              })
            : data === null
              ? NextResponse.json({ error: "Upstream error" }, { status: 502 })
              : NextResponse.json(data, { status: res.status });
      }
    }
  } catch (error) {
    response = NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 }
    );
  }
  timings.add("next_total", performance.now() - started);
  response.headers.set("Cache-Control", "no-store");
  response.headers.set(
    "Server-Timing",
    joinServerTimingHeaders(
      timings.toHeader(),
      upstream?.headers.get("server-timing")
    )!
  );
  return response;
}

export async function proxyPublicBackendJson({
  request,
  path,
  stream = false,
}: {
  request: Request;
  path: string;
  stream?: boolean;
}): Promise<NextResponse> {
  try {
    const res = await fetch(getBackendUrl(path), {
      cache: "no-store",
      signal: request.signal,
      headers: backendFetchHeaders(request),
    });
    if (stream) {
      return attachUpstreamServerTiming(
        new NextResponse(res.body, {
          status: res.status,
          headers: {
            "Content-Type":
              res.headers.get("content-type") ?? "application/json",
            "Cache-Control": "no-store",
          },
        }),
        res
      );
    }
    const { data, parseError, status } = await readBackendJson(
      res,
      "Upstream error"
    );
    const response = parseError
      ? NextResponse.json(parseError, { status })
      : !res.ok
        ? NextResponse.json(backendErrorPayload(data, "Upstream error"), {
            status: res.status,
          })
        : data === null
          ? NextResponse.json({ error: "Upstream error" }, { status: 502 })
          : NextResponse.json(data, { status: res.status });
    return attachUpstreamServerTiming(response, res);
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
