"use client";

import { useEffect, useRef } from "react";
import Link from "next/link";
import Image from "next/image";
import { usePathname } from "next/navigation";
import { useSWRConfig } from "swr";
import {
  OrganizationSwitcher,
  SignInButton,
  useClerk,
  useOrganization,
  useUser,
} from "@clerk/nextjs";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { ThemeToggle } from "@/components/theme-toggle";
import {
  BookOpen,
  ChevronDown,
  FileText,
  LogOut,
  SearchCheck,
  Shield,
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
    organizationSwitcherTrigger:
      "rounded-full border border-[#6f88b4]/20 bg-background/70 px-2.5 py-1.5 text-sm text-foreground hover:border-[#85b85c]/20 hover:bg-muted data-[state=open]:bg-muted",
    organizationPreviewMainIdentifier: "text-sm font-medium text-foreground",
    organizationSwitcherTriggerIcon: "text-muted-foreground",
    organizationSwitcherPopoverCard:
      "border border-border shadow-lg rounded-lg",
    organizationSwitcherPopoverActionButton: "text-foreground hover:bg-muted",
    avatarBox: "rounded-md border border-border",
  },
};

export function Nav() {
  const pathname = usePathname();
  const { user, isLoaded, isSignedIn } = useUser();
  const { signOut } = useClerk();
  const { organization } = useOrganization();
  const { mutate } = useSWRConfig();

  // All org-scoped SWR keys are plain URLs with no org id in them, so cached
  // responses from the previous workspace would otherwise survive a switch.
  const prevOrgId = useRef(organization?.id);
  useEffect(() => {
    if (
      prevOrgId.current !== undefined &&
      organization?.id !== prevOrgId.current
    ) {
      mutate(() => true, undefined, { revalidate: true });
    }
    prevOrgId.current = organization?.id;
  }, [organization?.id, mutate]);

  return (
    <nav className="sticky top-[var(--preview-banner-h,0px)] z-40 border-b border-[#6f88b4]/15 bg-card/80 backdrop-blur-xs">
      <div className="mx-auto flex h-14 max-w-(--breakpoint-2xl) items-center px-4">
        <div className="flex w-full items-center justify-between">
          {/* Left side - primary nav */}
          <div className="flex items-center gap-4">
            <Button
              variant={pathname === "/dashboard" ? "secondary" : "ghost"}
              size="sm"
              asChild
              className="gap-2 border border-transparent data-[active=true]:border-[#85b85c]/25"
            >
              <Link
                href="/dashboard"
                className="flex items-center gap-2"
                data-active={pathname === "/dashboard"}
              >
                <Image
                  src="/oddish.png"
                  alt="Oddish"
                  width={24}
                  height={24}
                  className="drop-shadow-xs"
                />
                <span>Dashboard</span>
              </Link>
            </Button>
            <Button
              variant={pathname === "/tasks" ? "secondary" : "ghost"}
              size="sm"
              asChild
              className="gap-2 border border-transparent data-[active=true]:border-[#85b85c]/25"
            >
              <Link
                href="/tasks"
                className="flex items-center gap-2"
                data-active={pathname === "/tasks"}
              >
                <FileText className="h-4 w-4" />
                <span>Tasks</span>
              </Link>
            </Button>
            <Button
              variant={pathname.startsWith("/qa") ? "secondary" : "ghost"}
              size="sm"
              asChild
              className="gap-2 border border-transparent data-[active=true]:border-[#85b85c]/25"
            >
              <Link
                href="/qa"
                className="flex items-center gap-2"
                data-active={pathname.startsWith("/qa")}
              >
                <SearchCheck className="h-4 w-4" />
                <span>Agents</span>
              </Link>
            </Button>
          </div>

          {/* Right side - consolidated settings menu */}
          <div className="flex items-center gap-2">
            <ThemeToggle />
            {isLoaded && isSignedIn && (
              <>
                <Button
                  variant="ghost"
                  size="sm"
                  asChild
                  className="gap-2 text-foreground hover:text-foreground"
                >
                  <a
                    href="https://github.com/abundant-ai/oddish/blob/main/DOCS.md"
                    target="_blank"
                    rel="noreferrer"
                  >
                    <BookOpen className="h-4 w-4" />
                    <span className="hidden sm:inline">Docs</span>
                  </a>
                </Button>
                <OrganizationSwitcher
                  hidePersonal
                  afterCreateOrganizationUrl="/dashboard"
                  afterSelectOrganizationUrl="/dashboard"
                  appearance={navSwitcherAppearance}
                />
                <DropdownMenu modal={false}>
                  <DropdownMenuTrigger asChild>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      className="h-auto rounded-full border border-[#6f88b4]/20 bg-background/70 px-2 py-1 text-sm hover:border-[#85b85c]/20 hover:bg-muted"
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
                      <ChevronDown className="hidden h-4 w-4 text-muted-foreground sm:inline" />
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
                      <p className="text-xs text-muted-foreground">
                        {user?.primaryEmailAddress?.emailAddress ?? "—"}
                      </p>
                    </div>
                    <DropdownMenuSeparator className="my-1" />
                    <DropdownMenuItem asChild>
                      <Link
                        href="/settings"
                        className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm outline-hidden hover:bg-muted focus:bg-muted"
                      >
                        <User className="h-4 w-4" />
                        Settings
                      </Link>
                    </DropdownMenuItem>
                    <DropdownMenuItem asChild>
                      <Link
                        href="/admin"
                        className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm outline-hidden hover:bg-muted focus:bg-muted"
                      >
                        <Shield className="h-4 w-4" />
                        Admin
                      </Link>
                    </DropdownMenuItem>
                    <DropdownMenuSeparator className="my-2" />
                    <DropdownMenuItem
                      onSelect={() => signOut()}
                      className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm text-red-500 outline-hidden hover:bg-muted focus:bg-muted"
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
