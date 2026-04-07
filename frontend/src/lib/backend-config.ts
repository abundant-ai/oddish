const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

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
    const templatedToken = await getToken({ template });
    if (templatedToken) {
      return templatedToken;
    }
  }

  return getToken();
}
