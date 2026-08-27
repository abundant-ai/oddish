import "server-only";

import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import type {
  ExperimentQaPatch,
  ExperimentQaReport,
  ExperimentShareInfo,
  PublicExperimentQaReport,
} from "@/lib/types";

export class ExperimentQaRequestError extends Error {
  constructor(
    message: string,
    public readonly status: number
  ) {
    super(message);
    this.name = "ExperimentQaRequestError";
  }
}

type ExperimentQaMutationBody =
  | ExperimentQaPatch
  | {
      expected_draft_version: number;
      expected_public_token?: string | null;
    };

function errorMessage(payload: unknown, fallback: string): string {
  if (!payload || typeof payload !== "object") return fallback;
  const value = payload as Record<string, unknown>;
  if (typeof value.detail === "string") return value.detail;
  if (typeof value.error === "string") return value.error;
  return fallback;
}

async function readPayload(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text.trim()) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new ExperimentQaRequestError(
      `The QA service returned an invalid response (${response.status}).`,
      response.ok ? 502 : response.status
    );
  }
}

async function privateRequest<T = ExperimentQaReport>(
  experimentId: string,
  method: "GET" | "POST" | "PATCH",
  suffix = "",
  body?: ExperimentQaMutationBody
): Promise<T> {
  const authObject = await auth();
  if (!authObject.userId) {
    throw new ExperimentQaRequestError("Sign in to view experiment QA.", 401);
  }
  const token = await getClerkToken(authObject.getToken);
  if (!token) {
    throw new ExperimentQaRequestError("Could not verify your session.", 401);
  }

  const response = await fetch(
    getBackendUrl(
      "experiments",
      `/${encodeURIComponent(experimentId)}/qa${suffix}`
    ),
    {
      method,
      cache: "no-store",
      headers: {
        ...getAuthHeaders(token),
        ...(body ? { "Content-Type": "application/json" } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    }
  );
  const payload = await readPayload(response);
  if (!response.ok) {
    throw new ExperimentQaRequestError(
      errorMessage(payload, "Experiment QA could not be updated."),
      response.status
    );
  }
  return payload as T;
}

export async function getExperimentQa(
  experimentId: string
): Promise<ExperimentQaReport | null> {
  try {
    return await privateRequest(experimentId, "GET");
  } catch (error) {
    if (error instanceof ExperimentQaRequestError && error.status === 404) {
      return null;
    }
    throw error;
  }
}

export async function createExperimentQa(
  experimentId: string
): Promise<ExperimentQaReport> {
  return privateRequest(experimentId, "POST");
}

export async function patchExperimentQa(
  experimentId: string,
  patch: ExperimentQaPatch
): Promise<ExperimentQaReport> {
  return privateRequest(experimentId, "PATCH", "", patch);
}

export async function syncExperimentQa(
  experimentId: string
): Promise<ExperimentQaReport> {
  return privateRequest(experimentId, "POST", "/sync");
}

export async function publishExperimentQa(
  experimentId: string,
  expectedDraftVersion: number,
  expectedPublicToken: string | null
): Promise<ExperimentQaReport> {
  return privateRequest(experimentId, "POST", "/publish", {
    expected_draft_version: expectedDraftVersion,
    expected_public_token: expectedPublicToken,
  });
}

export async function unpublishExperimentQa(
  experimentId: string,
  expectedDraftVersion: number,
  expectedPublicToken: string
): Promise<ExperimentQaReport> {
  return privateRequest(experimentId, "POST", "/unpublish", {
    expected_draft_version: expectedDraftVersion,
    expected_public_token: expectedPublicToken,
  });
}

export async function getExperimentQaPreview(
  experimentId: string
): Promise<PublicExperimentQaReport> {
  return privateRequest<PublicExperimentQaReport>(
    experimentId,
    "GET",
    "/preview"
  );
}

export async function getExperimentQaShareInfo(
  experimentId: string
): Promise<ExperimentShareInfo | null> {
  const authObject = await auth();
  if (!authObject.userId) return null;
  const token = await getClerkToken(authObject.getToken);
  if (!token) return null;
  const response = await fetch(
    getBackendUrl("experiments", `/${encodeURIComponent(experimentId)}/share`),
    { cache: "no-store", headers: getAuthHeaders(token) }
  );
  if (!response.ok) return null;
  return (await response.json()) as ExperimentShareInfo;
}

export async function getPublicExperimentQa(
  experimentToken: string,
  qaToken: string
): Promise<PublicExperimentQaReport> {
  const response = await fetch(
    getBackendUrl(
      "public/experiments",
      `/${encodeURIComponent(experimentToken)}/qa/${encodeURIComponent(qaToken)}`
    ),
    { cache: "no-store" }
  );
  const payload = await readPayload(response);
  if (!response.ok) {
    throw new ExperimentQaRequestError(
      errorMessage(payload, "This QA link is not available."),
      response.status
    );
  }
  return payload as PublicExperimentQaReport;
}
