const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

// Vercel exposes the deployment's target env (production / preview /
// development) on every build via this var. It's auto-set, no Vercel
// config needed. We read it server-side from the Next proxy routes to
// gate every backend call.
const VERCEL_ENV =
  process.env.NEXT_PUBLIC_VERCEL_ENV ?? process.env.VERCEL_ENV ?? null;

/** Modal preview apps follow ``abundant-ai-preview--oddish-pr-{N}-api``;
 * the ``--oddish-pr-`` marker is the unambiguous "this is a preview
 * backend, not production" signal. */
function isPerPrPreviewBackend(url: string): boolean {
  return /--oddish-pr-\d+-api\./.test(url);
}

/**
 * Hard refuse to call a non-preview backend from a Vercel preview
 * deployment. There's a window between Vercel auto-deploying the
 * frontend on git push and our workflow re-pointing
 * ``NEXT_PUBLIC_API_URL`` at the per-PR Modal app — during that
 * window the build can have inherited the project-level (production)
 * value. Sending mutating traffic to prod from a preview UI is the
 * worst kind of silent failure: it looks like the preview is working,
 * but every action is hitting the live database.
 *
 * We bail loudly here so the proxy returns 503 instead of forwarding
 * the call. ``frontend/src/components/preview-misconfig-banner.tsx``
 * surfaces the same check in the UI so a misconfigured preview is
 * immediately visible, not just on the next API call.
 */
export function assertSafeBackendUrl(url: string): void {
  if (VERCEL_ENV !== "preview") return;
  if (isPerPrPreviewBackend(url)) return;
  throw new Error(
    `[backend-config] Refusing to call "${url}" from a Vercel preview ` +
      `deployment. Preview frontends must talk to a per-PR Modal app ` +
      `(matching --oddish-pr-{N}-api), never production. The deploy ` +
      `workflow probably failed to repoint NEXT_PUBLIC_API_URL — see ` +
      `.github/workflows/modal-preview.yml ("Point Vercel preview to ` +
      `Modal preview" step).`,
  );
}

/** Inspectable view of the same check for UI banners. */
export function previewBackendStatus(): {
  vercelEnv: string | null;
  apiUrl: string;
  isPreviewDeployment: boolean;
  isMisconfigured: boolean;
} {
  return {
    vercelEnv: VERCEL_ENV,
    apiUrl: API_URL,
    isPreviewDeployment: VERCEL_ENV === "preview",
    isMisconfigured:
      VERCEL_ENV === "preview" && !isPerPrPreviewBackend(API_URL),
  };
}

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
  assertSafeBackendUrl(API_URL);

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
