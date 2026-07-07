const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export function getBackendUrl(
  endpoint: string,
  path: string = "",
  queryParams?: Record<string, string>,
): string {
  const query = queryParams ? new URLSearchParams(queryParams).toString() : "";
  return `${API_URL}/${endpoint}${path}${query ? `?${query}` : ""}`;
}

export function getAuthHeaders(clerkToken?: string | null): HeadersInit {
  return clerkToken
    ? {
        Authorization: `Bearer ${clerkToken}`,
        "X-Clerk-Authorization": `Bearer ${clerkToken}`,
      }
    : {};
}

type GetToken = (options?: { template?: string }) => Promise<string | null>;

const TOKEN_EXP_MARGIN_MS = 10_000;
const TOKEN_CACHE_MAX_SIZE = 500;
const tokenCache = new Map<string, { token: string; exp: number }>();
const pendingMints = new Map<string, Promise<string | null>>();

function decodeJwtPayload(token: string): Record<string, unknown> | null {
  try {
    return JSON.parse(
      atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")),
    ) as Record<string, unknown>;
  } catch {
    return null;
  }
}

function activeOrgId(payload: Record<string, unknown>): string {
  if (typeof payload.org_id === "string") return payload.org_id;
  const org = payload.o;
  if (!org || typeof org !== "object" || !("id" in org)) return "";
  return typeof org.id === "string" ? org.id : "";
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
    // Do not fall back to the default token; v2 tokens can lose org context.
    console.error(
      `Failed to mint Clerk token for template "${template}"`,
      error,
    );
    return null;
  }
}

export async function getClerkToken(
  getToken: GetToken,
): Promise<string | null> {
  const template = process.env.CLERK_JWT_TEMPLATE;
  if (!template) return getToken();

  const sessionToken = await getToken();
  if (!sessionToken) return null;
  const session = decodeJwtPayload(sessionToken);
  const sub = typeof session?.sub === "string" ? session.sub : null;
  if (!session || !sub) return mintTemplateToken(getToken, template, null);

  // Include org: Clerk v1 uses org_id, v2 uses o.id.
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
