// Backend configuration for switching between local FastAPI and Modal deployment

type BackendType = "local" | "modal";

const BACKEND_TYPE: BackendType =
  (process.env.NEXT_PUBLIC_BACKEND_TYPE as BackendType) || "local";

// Modal environment: "dev" adds "-dev" suffix to endpoint label, anything else uses prod URLs
// Default to "prod" to avoid accidentally hitting a non-existent dev endpoint
const MODAL_ENV = process.env.NEXT_PUBLIC_MODAL_ENV || "prod";

// Modal base URL (e.g., "https://your-workspace-12345")
// Must be set explicitly when NEXT_PUBLIC_BACKEND_TYPE=modal.
const MODAL_BASE_URL = process.env.NEXT_PUBLIC_MODAL_BASE_URL;

/**
 * Construct Modal URL for an endpoint
 * Dev mode: adds "-dev" suffix (e.g., api-dev)
 * Prod mode: no suffix (e.g., api)
 */
function getModalUrl(endpointName: string): string {
  // If explicitly set via env var, use that
  const envKey =
    `NEXT_PUBLIC_MODAL_${endpointName.toUpperCase().replace(/-/g, "_")}_URL` as const;
  const explicitUrl = process.env[envKey];
  if (explicitUrl) {
    return explicitUrl;
  }

  if (!MODAL_BASE_URL) {
    throw new Error(
      `Missing NEXT_PUBLIC_MODAL_BASE_URL (or ${envKey}) while NEXT_PUBLIC_BACKEND_TYPE=modal`,
    );
  }

  // Otherwise, construct URL based on MODAL_ENV
  const suffix = MODAL_ENV === "dev" ? "-dev" : "";
  const functionName = endpointName.replace(/_/g, "-");
  return `${MODAL_BASE_URL}--${functionName}${suffix}.modal.run`;
}

// Single Modal ASGI endpoint that serves all routes (see backend/endpoints.py)
const MODAL_API_URL = BACKEND_TYPE === "modal" ? getModalUrl("api") : "";

// Local FastAPI base URL
const LOCAL_FASTAPI_URL = process.env.FASTAPI_URL || "http://localhost:8000";

/**
 * Get the backend URL for a specific endpoint
 * @param endpoint - The endpoint name (e.g., 'health', 'tasks', 'queues')
 * @param path - Additional path parameters (e.g., '/123' for task ID)
 * @param queryParams - Optional query parameters
 * @returns The full URL to use for the API call
 */
export function getBackendUrl(
  endpoint: string,
  path: string = "",
  queryParams?: Record<string, string>,
): string {
  let baseUrl: string;
  const allQueryParams: Record<string, string> = { ...queryParams };

  if (BACKEND_TYPE === "modal") {
    baseUrl = `${MODAL_API_URL}/${endpoint}`;
  } else {
    // Local FastAPI
    baseUrl = `${LOCAL_FASTAPI_URL}/${endpoint}`;
  }

  // Build full URL with path
  let fullUrl = `${baseUrl}${path}`;

  // Add query parameters if provided
  if (Object.keys(allQueryParams).length > 0) {
    const params = new URLSearchParams(allQueryParams);
    fullUrl += `?${params.toString()}`;
  }

  return fullUrl;
}

/**
 * Get Authorization header for backend requests using Clerk token.
 * @param clerkToken - Clerk JWT token
 * @returns Headers object with Authorization header
 */
export function getAuthHeaders(clerkToken?: string | null): HeadersInit {
  if (clerkToken) {
    return {
      Authorization: `Bearer ${clerkToken}`,
      "X-Clerk-Authorization": `Bearer ${clerkToken}`,
    };
  }
  return {};
}

/**
 * Get a Clerk JWT, preferring a configured template when available.
 */
export async function getClerkToken(
  getToken: (options?: { template?: string }) => Promise<string | null>,
): Promise<string | null> {
  const template = process.env.CLERK_JWT_TEMPLATE;
  if (template) {
    const templatedToken = await getToken({ template });
    if (templatedToken) {
      return templatedToken;
    }
  }

  return getToken();
}
