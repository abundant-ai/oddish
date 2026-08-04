"use client";

import { useEffect, useRef } from "react";
import Link from "next/link";
import Image from "next/image";
import { usePathname } from "next/navigation";
import {
  OrganizationSwitcher,
  SignInButton,
  useAuth,
  useClerk,
  useOrganization,
  useUser,
} from "@clerk/nextjs";
import { isOrgAdminRole } from "@/lib/org-roles";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { NotificationsBell } from "@/components/comments/notifications-bell";
import { ThemeToggle } from "@/components/theme-toggle";
import {
  BookOpen,
  ChevronDown,
  FileBarChart,
  FileText,
  LogOut,
  Menu,
  SearchCheck,
  Shield,
  Trophy,
  User,
} from "lucide-react";

/**
 * Clerk's `OrganizationSwitcher` trigger sits inline in the nav bar;
 * we style it to match the surrounding nav controls.
 */
const navSwitcherAppearance = {
  variables: {
    colorBackground: "hsl(var(--card))",
    colorText: "hsl(var(--foreground))",
    colorTextSecondary: "hsl(var(--muted-foreground))",
    colorPrimary: "hsl(var(--primary))",
    colorInputBackground: "hsl(var(--background))",
    colorInputText: "hsl(var(--foreground))",
    colorNeutral: "hsl(var(--foreground))",
    borderRadius: "0.5rem",
    fontFamily: "var(--font-sans)",
  },
  elements: {
    rootBox: "flex items-center",
    // Capped on phones so a long org name truncates instead of widening the
    // bar. Every flex ancestor of the label needs min-w-0, or its min-content
    // width wins over the cap and the name overflows instead of truncating.
    organizationSwitcherTrigger:
      "max-w-[35vw] overflow-hidden rounded-full border border-[#6f88b4]/20 bg-background/70 px-2.5 py-1.5 text-sm text-foreground hover:border-[#85b85c]/20 hover:bg-muted data-[state=open]:bg-muted sm:max-w-none",
    organizationPreview: "min-w-0",
    organizationPreviewTextContainer: "min-w-0",
    organizationPreviewMainIdentifier:
      "truncate text-sm font-medium text-foreground",
    organizationSwitcherTriggerIcon: "text-muted-foreground",
    organizationSwitcherPopoverCard:
      "border border-border shadow-lg rounded-lg",
    organizationSwitcherPopoverActionButton: "text-foreground hover:bg-muted",
    avatarBox: "rounded-md border border-border",
  },
};

/**
 * @deprecated Agents and Analyzers are no longer shown in the primary header.
 * Keep their links here while the legacy routes remain directly accessible.
 */
const SHOW_DEPRECATED_AGENT_AND_ANALYZER_NAV = false;

const DOCS_URL = "https://github.com/abundant-ai/oddish/blob/main/DOCS.md";

type NavLink = {
  href: string;
  label: string;
  icon: React.ReactNode;
  prefix?: boolean;
};

const PRIMARY_NAV_LINKS: NavLink[] = [
  {
    href: "/dashboard",
    label: "Dashboard",
    icon: (
      <Image
        src="/oddish.png"
        alt=""
        width={24}
        height={24}
        className="drop-shadow-xs"
      />
    ),
  },
  { href: "/tasks", label: "Tasks", icon: <FileText className="h-4 w-4" /> },
  ...(SHOW_DEPRECATED_AGENT_AND_ANALYZER_NAV
    ? [
        {
          href: "/qa",
          label: "Agents",
          icon: <SearchCheck className="h-4 w-4" />,
          prefix: true,
        },
        {
          href: "/analyzers",
          label: "Analyzers",
          icon: <FileBarChart className="h-4 w-4" />,
          prefix: true,
        },
      ]
    : []),
  {
    href: "/leaderboard",
    label: "Leaderboard",
    icon: <Trophy className="h-4 w-4" />,
  },
];

function isNavLinkActive(pathname: string, link: NavLink): boolean {
  return link.prefix ? pathname.startsWith(link.href) : pathname === link.href;
}

export function Nav() {
  const pathname = usePathname();
  const { user, isLoaded, isSignedIn } = useUser();
  const { orgRole } = useAuth();
  const { signOut } = useClerk();
  const { organization } = useOrganization();
  const isOrgAdmin = isOrgAdminRole(orgRole);

  // Full reload on org switch. Org-scoped SWR keys and Next's client router
  // cache (RSC payloads, kept ~30s by staleTimes.dynamic) are both keyed on
  // plain URLs with no org id, so previous-workspace data would otherwise
  // survive a switch. router.refresh() clears only the current route, so a
  // hard navigation is the reliable way to drop every cached route at once.
  const prevOrgId = useRef(organization?.id);
  useEffect(() => {
    if (
      prevOrgId.current !== undefined &&
      organization?.id !== prevOrgId.current
    ) {
      window.location.assign("/dashboard");
      return;
    }
    prevOrgId.current = organization?.id;
  }, [organization?.id]);

  return (
    <nav className="bg-card/80 sticky top-[var(--preview-banner-h,0px)] z-40 border-b border-[#6f88b4]/15 backdrop-blur-xs">
      <div className="mx-auto flex h-14 max-w-(--breakpoint-2xl) items-center px-3 sm:px-4">
        <div className="flex w-full items-center justify-between">
          {/* Left side - primary nav */}
          <div className="flex min-w-0 items-center gap-2 sm:gap-4">
            <DropdownMenu modal={false}>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label="Open navigation"
                  className="px-2 sm:hidden"
                >
                  <Menu className="h-5 w-5" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent
                align="start"
                className="w-56 border-[#6f88b4]/20 p-2"
              >
                {PRIMARY_NAV_LINKS.map((link) => (
                  <DropdownMenuItem key={link.href} asChild>
                    <Link
                      href={link.href}
                      data-active={isNavLinkActive(pathname, link)}
                      className="hover:bg-muted focus:bg-muted data-[active=true]:bg-muted flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm outline-hidden"
                    >
                      {link.icon}
                      {link.label}
                    </Link>
                  </DropdownMenuItem>
                ))}
                <DropdownMenuSeparator className="my-1" />
                <DropdownMenuItem asChild>
                  <a
                    href={DOCS_URL}
                    target="_blank"
                    rel="noreferrer"
                    className="hover:bg-muted focus:bg-muted flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm outline-hidden"
                  >
                    <BookOpen className="h-4 w-4" />
                    Docs
                  </a>
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
            <Link href="/dashboard" className="shrink-0 sm:hidden">
              <Image
                src="/oddish.png"
                alt="Oddish"
                width={24}
                height={24}
                className="drop-shadow-xs"
              />
            </Link>
            <div className="hidden items-center gap-4 sm:flex">
              {PRIMARY_NAV_LINKS.map((link) => {
                const active = isNavLinkActive(pathname, link);
                return (
                  <Button
                    key={link.href}
                    variant={active ? "secondary" : "ghost"}
                    size="sm"
                    asChild
                    className="gap-2 border border-transparent data-[active=true]:border-[#85b85c]/25"
                  >
                    <Link
                      href={link.href}
                      className="flex items-center gap-2"
                      data-active={active}
                    >
                      {link.icon}
                      <span>{link.label}</span>
                    </Link>
                  </Button>
                );
              })}
            </div>
          </div>

          {/* Right side - consolidated settings menu */}
          <div className="flex items-center gap-2">
            <ThemeToggle />
            {isLoaded && isSignedIn && (
              <>
                <NotificationsBell />
                <Button
                  variant="ghost"
                  size="sm"
                  asChild
                  className="text-foreground hover:text-foreground hidden gap-2 sm:inline-flex"
                >
                  <a href={DOCS_URL} target="_blank" rel="noreferrer">
                    <BookOpen className="h-4 w-4" />
                    <span>Docs</span>
                  </a>
                </Button>
                {/* No afterSelect/afterCreateOrganizationUrl: Clerk's
                    client-side navigation would render /dashboard from the
                    URL-keyed router cache (previous org's payload) before the
                    org-change effect below fires its hard reload. Routing the
                    switch solely through that reload avoids the stale flash. */}
                <OrganizationSwitcher
                  hidePersonal
                  appearance={navSwitcherAppearance}
                />
                <DropdownMenu modal={false}>
                  <DropdownMenuTrigger asChild>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      className="bg-background/70 hover:bg-muted h-auto rounded-full border border-[#6f88b4]/20 px-2 py-1 text-sm hover:border-[#85b85c]/20"
                    >
                      <Avatar className="h-8 w-8">
                        <AvatarImage
                          src={user?.imageUrl}
                          alt={user?.fullName ?? "User avatar"}
                        />
                        <AvatarFallback className="text-xs font-semibold">
                          {user?.firstName?.[0] ?? "U"}
                        </AvatarFallback>
                      </Avatar>
                      <span className="hidden md:inline">
                        {user?.firstName ?? user?.fullName ?? "Account"}
                      </span>
                      <ChevronDown className="text-muted-foreground hidden h-4 w-4 sm:inline" />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent
                    align="end"
                    className="w-64 border-[#6f88b4]/20 p-2"
                  >
                    <div className="px-2 py-1.5">
                      <p className="text-sm font-medium">
                        {user?.fullName ?? user?.username ?? "Account"}
                      </p>
                      <p className="text-muted-foreground text-xs">
                        {user?.primaryEmailAddress?.emailAddress ?? "—"}
                      </p>
                    </div>
                    <DropdownMenuSeparator className="my-1" />
                    <DropdownMenuItem asChild>
                      <Link
                        href="/settings"
                        className="hover:bg-muted focus:bg-muted flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm outline-hidden"
                      >
                        <User className="h-4 w-4" />
                        Settings
                      </Link>
                    </DropdownMenuItem>
                    {isOrgAdmin && (
                      <DropdownMenuItem asChild>
                        <Link
                          href="/admin"
                          className="hover:bg-muted focus:bg-muted flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm outline-hidden"
                        >
                          <Shield className="h-4 w-4" />
                          Admin
                        </Link>
                      </DropdownMenuItem>
                    )}
                    <DropdownMenuSeparator className="my-2" />
                    <DropdownMenuItem
                      onSelect={() => signOut()}
                      className="hover:bg-muted focus:bg-muted flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm text-red-500 outline-hidden"
                    >
                      <LogOut className="h-4 w-4" />
                      Sign out
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              </>
            )}
            {isLoaded && !isSignedIn && (
              <SignInButton mode="modal" fallbackRedirectUrl="/dashboard">
                <Button variant="outline" size="sm">
                  Sign in
                </Button>
              </SignInButton>
            )}
          </div>
        </div>
      </div>
    </nav>
  );
}
