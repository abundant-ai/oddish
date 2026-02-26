"use client";

import Link from "next/link";
import Image from "next/image";
import { usePathname } from "next/navigation";
import {
  OrganizationSwitcher,
  SignedIn,
  SignedOut,
  SignInButton,
  useClerk,
  useOrganization,
  useUser,
} from "@clerk/nextjs";
import useSWR from "swr";
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
import { ChevronDown, Key, User, LogOut, Activity, Shield } from "lucide-react";
import { fetcher } from "@/lib/api";

type HealthResponse = {
  status?: string;
};

function HealthIndicator() {
  const { data, error, isLoading } = useSWR<HealthResponse>(
    "/api/health",
    fetcher,
    {
      refreshInterval: 30000,
      revalidateOnFocus: false,
    },
  );

  const status = data?.status;

  let statusText = "Loading...";
  let colorClass = "text-yellow-400";

  if (error) {
    statusText = "Disconnected";
    colorClass = "text-red-400";
  } else if (status === "healthy") {
    statusText = "Healthy";
    colorClass = "text-green-400";
  } else if (status) {
    statusText = status;
    colorClass = "text-yellow-400";
  } else if (!isLoading && !status) {
    statusText = "Unknown";
    colorClass = "text-yellow-400";
  }

  return (
    <div className="flex items-center gap-1.5 px-2 py-1 rounded-md bg-muted/50">
      <Activity className={`h-3.5 w-3.5 ${colorClass}`} />
      <span className={`text-xs font-medium capitalize ${colorClass}`}>
        {statusText}
      </span>
    </div>
  );
}

export function Nav() {
  const pathname = usePathname();
  const { user } = useUser();
  const { organization } = useOrganization();
  const { signOut } = useClerk();

  return (
    <nav className="border-b border-border bg-card/70 backdrop-blur-sm sticky top-0 z-50">
      <div className="max-w-screen-2xl mx-auto px-4 h-14 flex items-center">
        <div className="flex items-center justify-between w-full">
          {/* Left side - primary nav */}
          <div className="flex items-center gap-4">
            <Button
              variant={pathname === "/dashboard" ? "secondary" : "ghost"}
              size="sm"
              asChild
              className="gap-2"
            >
              <Link href="/dashboard" className="flex items-center gap-2">
                <Image
                  src="/oddish.jpg"
                  alt="Oddish"
                  width={24}
                  height={24}
                  className="drop-shadow-sm"
                />
                <span>Dashboard</span>
              </Link>
            </Button>
            <Button
              variant={pathname.startsWith("/datasets") ? "secondary" : "ghost"}
              size="sm"
              asChild
              className="gap-2"
            >
              <Link href="/datasets" className="flex items-center gap-2">
                <span>Datasets</span>
              </Link>
            </Button>
          </div>

          {/* Right side - consolidated settings menu */}
          <div className="flex items-center gap-2">
            <ThemeToggle />
            <SignedIn>
              <HealthIndicator />
              <DropdownMenu modal={false}>
                <DropdownMenuTrigger asChild>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="h-auto rounded-full border border-border bg-background/60 px-2 py-1 text-sm hover:bg-muted"
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
                      {organization?.name ?? user?.fullName ?? "Account"}
                    </span>
                    <ChevronDown className="hidden h-4 w-4 text-muted-foreground sm:inline" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="w-64 p-2">
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
                      className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm outline-none hover:bg-muted focus:bg-muted"
                    >
                      <User className="h-4 w-4" />
                      Settings
                    </Link>
                  </DropdownMenuItem>
                  <DropdownMenuItem asChild>
                    <Link
                      href="/settings?tab=api-keys"
                      className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm outline-none hover:bg-muted focus:bg-muted"
                    >
                      <Key className="h-4 w-4" />
                      API keys
                    </Link>
                  </DropdownMenuItem>
                  <DropdownMenuItem asChild>
                    <Link
                      href="/admin"
                      className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm outline-none hover:bg-muted focus:bg-muted"
                    >
                      <Shield className="h-4 w-4" />
                      Admin
                    </Link>
                  </DropdownMenuItem>
                  <DropdownMenuSeparator className="my-2" />
                  <div className="space-y-2 px-2 py-1">
                    <p className="text-xs font-semibold uppercase text-muted-foreground">
                      Organization
                    </p>
                    <OrganizationSwitcher
                      appearance={{
                        elements: {
                          rootBox: "flex items-center",
                          organizationSwitcherTrigger:
                            "w-full justify-between rounded-md border border-border bg-muted/70 px-3 py-2 text-sm font-medium text-foreground shadow-sm hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
                        },
                      }}
                    />
                  </div>
                  <DropdownMenuSeparator className="my-2" />
                  <DropdownMenuItem
                    onSelect={() => signOut()}
                    className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm text-red-500 outline-none hover:bg-muted focus:bg-muted"
                  >
                    <LogOut className="h-4 w-4" />
                    Sign out
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            </SignedIn>
            <SignedOut>
              <SignInButton mode="modal" fallbackRedirectUrl="/dashboard">
                <Button variant="outline" size="sm">
                  Sign in
                </Button>
              </SignInButton>
            </SignedOut>
          </div>
        </div>
      </div>
    </nav>
  );
}
