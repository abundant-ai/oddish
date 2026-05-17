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
