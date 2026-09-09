/**
 * Authenticated dashboard URLs are `/orgs/{orgSlug}/tasks`,
 * `/orgs/{orgSlug}/dashboard`, and so on. Page files stay at `/tasks`,
 * `/dashboard`, …; middleware rewrites the slugged URL onto those routes and
 * redirects the old unprefixed ones (and the short-lived `/{slug}/…` shape).
 *
 * Public surfaces (`/share`, `/datasets`, `/sign-in`, `/sign-up`, `/api`) stay
 * unprefixed. `/experiments` is also left unprefixed for link-unfurl bots;
 * signed-in users are redirected to `/orgs/{slug}/experiments/…`, and that
 * slugged path stays public so pasting the address-bar URL still unfurls.
 */

export const ORG_PREFIX = "orgs";

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

/** Clerk org slugs are URL-safe. `/orgs` and public roots cannot be slugs. */
const SLUG_RE = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$/;

export const ORG_SYNC_PATTERNS = [
  `/${ORG_PREFIX}/:slug`,
  `/${ORG_PREFIX}/:slug/(.*)`,
] as const;

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

/** `/orgs/{slug}/experiments/…` stays public so Slack/Twitter unfurls work. */
export function isSluggedExperimentPath(pathname: string): boolean {
  if (!parseOrgSlug(pathname)) return false;
  return firstSegment(stripOrgSlug(pathname)) === "experiments";
}

export function isOrgSlug(segment: string): boolean {
  return (
    SLUG_RE.test(segment) &&
    segment !== ORG_PREFIX &&
    !PUBLIC_ROOT.has(segment)
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

function pathSegments(pathname: string): string[] {
  return pathname.split("/").filter(Boolean);
}

export function orgScopedPath(slug: string, appPath = "/dashboard"): string {
  const normalized = appPath === "/" ? "" : appPath;
  return `/${ORG_PREFIX}/${slug}${normalized}`;
}

export function parseOrgSlug(pathname: string): string | null {
  const parts = pathSegments(pathname);
  if (parts[0] !== ORG_PREFIX) return null;
  const slug = parts[1];
  if (!slug || !isOrgSlug(slug)) return null;
  return slug;
}

export function stripOrgSlug(pathname: string): string {
  const slug = parseOrgSlug(pathname);
  if (!slug) return pathname || "/";
  const rest = pathname.slice(`/${ORG_PREFIX}/${slug}`.length);
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
    return `${orgScopedPath(slug, stripOrgSlug(pathname))}${suffix}`;
  }
  if (isAppRootSegment(first)) {
    return `${orgScopedPath(slug, pathname)}${suffix}`;
  }
  return href;
}

export function resolveOrgRequest(input: {
  pathname: string;
  userId: string | null | undefined;
  orgSlug: string | null | undefined;
}): OrgRequestDecision {
  const { pathname, userId, orgSlug } = input;
  const parts = pathSegments(pathname);
  const first = parts[0];

  if (!first) {
    if (userId && orgSlug) {
      return {
        action: "redirect",
        pathname: orgScopedPath(orgSlug),
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
        pathname: orgScopedPath(orgSlug, pathname),
        // Temporary: the target org is session-dependent. A 308 would pin
        // the previous workspace in the browser cache.
        status: 307,
      };
    }
    return { action: "next" };
  }

  if (first === ORG_PREFIX) {
    const slug = parts[1];
    if (!slug) {
      if (userId && orgSlug) {
        return {
          action: "redirect",
          pathname: orgScopedPath(orgSlug),
          status: 307,
        };
      }
      return { action: "next" };
    }
    if (!isOrgSlug(slug)) return { action: "next" };
    const rest = stripOrgSlug(pathname);
    if (rest === "/") {
      return {
        action: "redirect",
        pathname: orgScopedPath(slug),
        status: 307,
      };
    }
    return { action: "rewrite", pathname: rest };
  }

  // Previous `/{slug}/…` bookmarks move under `/orgs/{slug}/…`.
  if (isOrgSlug(first)) {
    const restParts = parts.slice(1);
    const appPath =
      restParts.length === 0 ? "/dashboard" : `/${restParts.join("/")}`;
    return {
      action: "redirect",
      pathname: orgScopedPath(first, appPath),
      status: 308,
    };
  }

  return { action: "next" };
}
