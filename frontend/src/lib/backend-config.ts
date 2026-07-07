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

type GetToken = (options?: { template?: string }) => Promise<string | null>;

// Minting a templated token is an uncached POST to Clerk's API on every call
// (@clerk/backend SessionAPI.getToken), so cache minted tokens per
// user+org+template until just before `exp`. The backend validates tokens by
// signature + exp only, so a cached still-valid token is indistinguishable
// from a fresh one. Per-process cache; a cold instance just mints once.
const TOKEN_EXP_MARGIN_MS = 10_000;
const TOKEN_CACHE_MAX_SIZE = 500;
const tokenCache = new Map<string, { token: string; exp: number }>();
const pendingMints = new Map<string, Promise<string | null>>();

function decodeJwtPayload(token: string): Record<string, unknown> | null {
  try {
    const segment = token.split(".")[1];
    if (!segment) return null;
    const base64 = segment.replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(atob(base64)) as Record<string, unknown>;
  } catch {
    return null;
  }
}

// Active org lives at `org_id` in v1 Clerk session tokens and `o.id` in v2.
function activeOrgId(payload: Record<string, unknown>): string {
  if (typeof payload.org_id === "string") return payload.org_id;
  const o = payload.o;
  if (
    o &&
    typeof o === "object" &&
    typeof (o as { id?: unknown }).id === "string"
  ) {
    return (o as { id: string }).id;
  }
  return "";
}

async function mintTemplateToken(
  getToken: GetToken,
  template: string,
  cacheKey: string | null,
): Promise<string | null> {
  try {
    const token = await getToken({ template });
    if (!token) return null;
    const exp = decodeJwtPayload(token)?.exp;
    const expMs = typeof exp === "number" ? exp * 1000 : 0;
    if (cacheKey && expMs > Date.now() + TOKEN_EXP_MARGIN_MS) {
      if (tokenCache.size >= TOKEN_CACHE_MAX_SIZE) tokenCache.clear();
      tokenCache.set(cacheKey, { token, exp: expMs });
    }
    return token;
  } catch (error) {
    // No fallback to the default session token: the backend reads the
    // top-level `org_id` claim only, and a v2 session token nests org under
    // `o.id`, so falling back can silently reach the backend org-less.
    console.error(
      `Failed to mint Clerk token for template "${template}"`,
      error,
    );
    return null;
  }
}

/**
 * Get a Clerk JWT for backend requests, minting via the configured template
 * and caching the minted token until just before it expires.
 */
export async function getClerkToken(
  getToken: GetToken,
): Promise<string | null> {
  const template = process.env.CLERK_JWT_TEMPLATE;
  if (!template) return getToken();

  // The bare session token is returned from memory (no network); its claims
  // identify the user + active org for the cache key.
  const sessionToken = await getToken();
  if (!sessionToken) return null;
  const session = decodeJwtPayload(sessionToken);
  const sub = typeof session?.sub === "string" ? session.sub : null;
  if (!session || !sub) return mintTemplateToken(getToken, template, null);

  const cacheKey = `${sub}:${activeOrgId(session)}:${template}`;
  const hit = tokenCache.get(cacheKey);
  if (hit && hit.exp - Date.now() > TOKEN_EXP_MARGIN_MS) return hit.token;

  const pending = pendingMints.get(cacheKey);
  if (pending) return pending;
  const mint = mintTemplateToken(getToken, template, cacheKey).finally(() => {
    pendingMints.delete(cacheKey);
  });
  pendingMints.set(cacheKey, mint);
  return mint;
}
