/**
 * Authenticated dashboard URLs are `/{orgSlug}/tasks`, `/{orgSlug}/dashboard`,
 * and so on. Page files stay at `/tasks`, `/dashboard`, …; middleware rewrites
 * the slugged URL onto those routes and redirects the old unprefixed ones.
 *
 * Public surfaces (`/share`, `/datasets`, `/sign-in`, `/sign-up`, `/api`) stay
 * unprefixed. `/experiments` is also left unprefixed for link-unfurl bots;
 * signed-in users are redirected to the slugged form.
 */

export const PUBLIC_ROOT_SEGMENTS = [
  "sign-in",
  "sign-up",
  "share",
  "datasets",
  "api",
] as const;

export const APP_ROOT_SEGMENTS = [
  "dashboard",
  "tasks",
  "experiments",
  "deliveries",
  "qa",
  "leaderboard",
  "settings",
  "admin",
  "usage",
  "skills",
  "documents",
] as const;

const PUBLIC_ROOT = new Set<string>(PUBLIC_ROOT_SEGMENTS);
const APP_ROOT = new Set<string>(APP_ROOT_SEGMENTS);

/** Clerk org slugs are URL-safe; reject reserved first segments as slugs. */
const SLUG_RE = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$/;

export const ORG_SYNC_PATTERNS: string[] = APP_ROOT_SEGMENTS.flatMap(
  (segment) => [`/:slug/${segment}`, `/:slug/${segment}/(.*)`],
);

export type OrgRequestDecision =
  | { action: "next" }
  | { action: "redirect"; pathname: string; status: 307 | 308 }
  | { action: "rewrite"; pathname: string };

export function isPublicRootSegment(segment: string): boolean {
  return PUBLIC_ROOT.has(segment);
}

export function isAppRootSegment(segment: string): boolean {
  return APP_ROOT.has(segment);
}

export function isOrgSlug(segment: string): boolean {
  return (
    SLUG_RE.test(segment) &&
    !PUBLIC_ROOT.has(segment) &&
    !APP_ROOT.has(segment)
  );
}

export function splitHref(href: string): { pathname: string; suffix: string } {
  const queryAt = href.indexOf("?");
  const hashAt = href.indexOf("#");
  let cut = href.length;
  if (queryAt >= 0) cut = Math.min(cut, queryAt);
  if (hashAt >= 0) cut = Math.min(cut, hashAt);
  return { pathname: href.slice(0, cut), suffix: href.slice(cut) };
}

export function firstSegment(pathname: string): string | null {
  const segment = pathname.split("/").find((part) => part.length > 0);
  return segment ?? null;
}

export function parseOrgSlug(pathname: string): string | null {
  const parts = pathname.split("/").filter(Boolean);
  const first = parts[0];
  if (!first || !isOrgSlug(first)) return null;
  return first;
}

export function stripOrgSlug(pathname: string): string {
  const slug = parseOrgSlug(pathname);
  if (!slug) return pathname || "/";
  const rest = pathname.slice(`/${slug}`.length);
  return rest === "" ? "/" : rest;
}

export function withOrgSlug(
  href: string,
  slug: string | null | undefined,
): string {
  const { pathname, suffix } = splitHref(href);
  if (!slug || !pathname.startsWith("/")) return href;

  const first = firstSegment(pathname);
  if (!first) return href;
  if (isPublicRootSegment(first)) return href;

  const existing = parseOrgSlug(pathname);
  if (existing) {
    const appPath = stripOrgSlug(pathname);
    return `/${slug}${appPath === "/" ? "" : appPath}${suffix}`;
  }
  if (isAppRootSegment(first)) {
    return `/${slug}${pathname}${suffix}`;
  }
  return href;
}

export function resolveOrgRequest(input: {
  pathname: string;
  userId: string | null | undefined;
  orgSlug: string | null | undefined;
}): OrgRequestDecision {
  const { pathname, userId, orgSlug } = input;
  const first = firstSegment(pathname);

  if (!first) {
    if (userId && orgSlug) {
      return {
        action: "redirect",
        pathname: `/${orgSlug}/dashboard`,
        status: 307,
      };
    }
    if (userId) {
      return { action: "redirect", pathname: "/dashboard", status: 307 };
    }
    return { action: "next" };
  }

  if (isPublicRootSegment(first)) return { action: "next" };

  if (isAppRootSegment(first)) {
    if (userId && orgSlug) {
      return {
        action: "redirect",
        pathname: `/${orgSlug}${pathname}`,
        status: 308,
      };
    }
    return { action: "next" };
  }

  if (!isOrgSlug(first)) return { action: "next" };

  const rest = stripOrgSlug(pathname);
  if (rest === "/") {
    return {
      action: "redirect",
      pathname: `/${first}/dashboard`,
      status: 307,
    };
  }
  return { action: "rewrite", pathname: rest };
}
