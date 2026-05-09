// Resolution order for the API base URL (server-side, in proxy routes):
//
//  1. ``NEXT_PUBLIC_API_URL`` -- explicit override. Local dev uses this to
//     point at ``http://localhost:8000`` (uvicorn) or a Modal serve URL.
//     Production may set this to the public ``/py-api`` URL on the
//     custom domain (e.g. ``https://oddish.app/py-api``) so server-side
//     ``fetch`` calls hit the same Vercel deployment as the SPA.
//
//  2. ``VERCEL_URL`` -- automatic fallback when running on Vercel and no
//     override is configured. Each deployment gets its own ``VERCEL_URL``
//     (e.g. ``oddish-abc123.vercel.app``); routing internal calls through
//     it guarantees the proxy hits the *same* deployment as the frontend
//     bundle that originated the request, which is what makes the
//     migration's atomic-deploy guarantee actually hold.
//
//  3. ``http://localhost:8000`` -- legacy local default for ``uvicorn
//     serve:api``.
function resolveApiUrl(): string {
  const explicit = process.env.NEXT_PUBLIC_API_URL;
  if (explicit) {
    return explicit.replace(/\/+$/, "");
  }
  const vercelUrl = process.env.VERCEL_URL;
  if (vercelUrl) {
    return `https://${vercelUrl}/py-api`;
  }
  return "http://localhost:8000";
}

const API_URL = resolveApiUrl();

/**
 * Get the backend URL for a specific endpoint.
 * @param endpoint - The endpoint name (e.g., 'dashboard', 'tasks', 'queues')
 * @param path - Additional path parameters (e.g., '/123' for task ID)
 * @param queryParams - Optional query parameters
 * @returns The full URL to use for the API call
 */
export function getBackendUrl(
  endpoint: string,
  path: string = "",
  queryParams?: Record<string, string>,
): string {
  let fullUrl = `${API_URL}/${endpoint}${path}`;

  if (queryParams && Object.keys(queryParams).length > 0) {
    fullUrl += `?${new URLSearchParams(queryParams).toString()}`;
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
    try {
      const templatedToken = await getToken({ template });
      if (templatedToken) {
        return templatedToken;
      }
    } catch (error) {
      console.warn(
        `Failed to get Clerk token for template "${template}", falling back to the default session token.`,
        error,
      );
    }
  }

  return getToken();
}
