"use client";

import Link from "next/link";
import Image from "next/image";
import { usePathname } from "next/navigation";
import {
  SignedIn,
  SignedOut,
  SignInButton,
  useClerk,
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
import { ChevronDown, User, LogOut, Shield, BookOpen } from "lucide-react";

export function Nav() {
  const pathname = usePathname();
  const { user } = useUser();
  const { signOut } = useClerk();

  return (
    <nav className="sticky top-0 z-50 border-b border-[#6f88b4]/15 bg-card/80 backdrop-blur-sm">
      <div className="mx-auto flex h-14 max-w-screen-2xl items-center px-4">
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
                  className="drop-shadow-sm"
                />
                <span>Dashboard</span>
              </Link>
            </Button>
          </div>

          {/* Right side - consolidated settings menu */}
          <div className="flex items-center gap-2">
            <ThemeToggle />
            <SignedIn>
              <Button
                variant="ghost"
                size="sm"
                asChild
                className="gap-2 text-[#5d77a5] hover:text-[#526a95] dark:text-[#a8b8d2] dark:hover:text-[#c0cde1]"
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
                      className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm outline-none hover:bg-muted focus:bg-muted"
                    >
                      <User className="h-4 w-4" />
                      Settings
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
