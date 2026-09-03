// ---------------------------------------------------------------------------
// Direct mode: the browser calls the backend itself.
//
// By default every dashboard request goes browser -> Vercel edge -> Vercel
// function (a /api/* route handler that verifies the Clerk session, mints a
// backend token and forwards) -> Modal. That hop costs 0.3-0.6 s per call and
// splits the trace. With NEXT_PUBLIC_API_DIRECT=1 the same request goes
// browser -> Modal, with the token minted by the Clerk client. The SWR keys
// stay "/api/..." strings, so nothing about caching or invalidation changes;
// only the URL that is fetched does.
// ---------------------------------------------------------------------------

// Proxies that do more than forward the request keep serving in direct mode:
// the Logfire trace relay holds a write token the browser must not see, and
// the zip import repackages the upload before forwarding it.
const KEEP_PROXY = ["/api/client-traces", "/api/imports/zip"];

// Proxies whose backend path differs from their /api path.
const REWRITES: Array<[RegExp, string]> = [
  [/^\/api\/settings\/account(?=[/?#]|$)/, "/users/me"],
  [/^\/api\/settings\/api-keys(?=[/?#]|$)/, "/api-keys"],
  [/^\/api\/settings\/byok(?=[/?#]|$)/, "/byok"],
  [/^\/api\/settings\/notifications(?=[/?#]|$)/, "/users/me/alert-preferences"],
  [
    /^\/api\/admin\/users\/([^/?#]+)\/costs(?=[/?#]|$)/,
    "/admin/costs/users/$1",
  ],
];

export type ApiEnv = {
  NEXT_PUBLIC_API_DIRECT?: string;
  NEXT_PUBLIC_API_URL?: string;
};

export function directApiEnabled(env: ApiEnv = process.env as ApiEnv): boolean {
  const flag = (env.NEXT_PUBLIC_API_DIRECT ?? "").trim().toLowerCase();
  return (flag === "1" || flag === "true") && Boolean(env.NEXT_PUBLIC_API_URL);
}

export type ResolvedApiUrl = {
  url: string;
  /** True when the request bypasses the /api proxy. */
  direct: boolean;
  /** True for share-page reads that carry no token. */
  public: boolean;
};

/**
 * Map a dashboard request URL ("/api/<rest>") to where it should be sent.
 *
 * Returns the input unchanged unless direct mode is on, the code is running
 * in a browser, and the path is one the backend serves at the same (or a
 * rewritten) location. Anything else -- absolute URLs, the two relays above,
 * server-side rendering -- keeps the proxy.
 */
export function resolveApiUrl(
  input: string,
  {
    env = process.env as ApiEnv,
    inBrowser = typeof window !== "undefined",
  }: { env?: ApiEnv; inBrowser?: boolean } = {}
): ResolvedApiUrl {
  const fallback = { url: input, direct: false, public: false };
  if (!inBrowser || !directApiEnabled(env)) return fallback;
  if (!input.startsWith("/api/")) return fallback;
  if (
    KEEP_PROXY.some(
      (prefix) =>
        input === prefix ||
        input.startsWith(`${prefix}/`) ||
        input.startsWith(`${prefix}?`)
    )
  ) {
    return fallback;
  }
  const base = (env.NEXT_PUBLIC_API_URL ?? "").replace(/\/+$/, "");
  let path = input.slice("/api".length);
  for (const [pattern, replacement] of REWRITES) {
    if (pattern.test(input)) {
      path = input.replace(pattern, replacement);
      break;
    }
  }
  return {
    url: `${base}${path}`,
    direct: true,
    public: path.startsWith("/public/"),
  };
}

type ClerkSession = {
  getToken: (options?: { template?: string }) => Promise<string | null>;
};

type ClerkGlobal = { loaded?: boolean; session?: ClerkSession | null };

function clerkSession(): ClerkSession | null {
  const clerk = (globalThis as { Clerk?: ClerkGlobal }).Clerk;
  if (!clerk?.loaded || !clerk.session) return null;
  return clerk.session;
}

/**
 * The backend token for the signed-in user, or null while Clerk is loading.
 *
 * Same template the proxy routes mint server-side (`CLERK_JWT_TEMPLATE`),
 * published to the browser as `NEXT_PUBLIC_CLERK_JWT_TEMPLATE`. Clerk's
 * client caches the minted token for its lifetime and coalesces concurrent
 * requests, so this costs one round trip per token lifetime, not per call.
 */
async function clientToken(): Promise<string | null> {
  const session = clerkSession();
  if (!session) return null;
  const template = process.env.NEXT_PUBLIC_CLERK_JWT_TEMPLATE;
  try {
    return await session.getToken(template ? { template } : undefined);
  } catch {
    return null;
  }
}

/**
 * Fetch a dashboard API URL, going straight to the backend in direct mode.
 *
 * Drop-in for `fetch("/api/...")`: the token comes from the Clerk client
 * (cached for its lifetime by `getClerkToken`), and a request that cannot
 * get one yet -- Clerk still loading -- keeps using the proxy for that call
 * rather than failing.
 */
export async function apiFetch(
  input: string,
  init: RequestInit = {}
): Promise<Response> {
  const target = resolveApiUrl(input);
  if (!target.direct) return fetch(input, init);

  const headers = new Headers(init.headers);
  if (!target.public) {
    const token = await clientToken();
    if (!token) return fetch(input, init);
    headers.set("Authorization", `Bearer ${token}`);
  }
  return fetch(target.url, {
    ...init,
    headers,
    // Cross-origin: the bearer token carries the identity, cookies do not.
    credentials: "omit",
  });
}

export const fetcher = async <T>(
  url: string,
  init?: RequestInit
): Promise<T> => {
  const res = await apiFetch(url, { credentials: "include", ...init });
  let data: unknown = null;

  try {
    data = await res.json();
  } catch {
    data = null;
  }

  if (!res.ok) {
    const errorData =
      typeof data === "object" && data
        ? (data as { detail?: unknown; error?: unknown })
        : null;
    const message =
      (typeof errorData?.detail === "string" && errorData.detail) ||
      (typeof errorData?.error === "string" && errorData.error) ||
      res.statusText ||
      "Request failed";
    const err = new Error(message);
    (err as Error & { status?: number; info?: unknown }).status = res.status;
    (err as Error & { status?: number; info?: unknown }).info = data;
    throw err;
  }

  return data as T;
};

// Format Harbor stage for display
export function formatHarborStage(stage: string | null | undefined): string {
  if (!stage) return "Pending";

  const stageMap: Record<string, string> = {
    starting: "Initializing",
    trial_started: "Starting",
    environment_setup: "Environment Setup",
    agent_running: "Agent Running",
    verification: "Verification",
    completed: "Completed",
    cleanup: "Cleanup",
    cancelled: "Cancelled",
  };

  return (
    stageMap[stage] ||
    stage.replace(/_/g, " ").replace(/\b\w/g, (l) => l.toUpperCase())
  );
}
