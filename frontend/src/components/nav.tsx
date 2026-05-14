import Link from "next/link";
import Image from "next/image";
import { Show, SignInButton } from "@clerk/nextjs";
import { currentUser } from "@clerk/nextjs/server";
import { Button } from "@/components/ui/button";
import { ThemeToggle } from "@/components/theme-toggle";
import { BookOpen, FileText } from "lucide-react";
import { NavLink } from "@/components/nav-link";
import { UserMenu } from "@/components/user-menu";

export async function Nav() {
  const user = await currentUser();

  return (
    <nav className="sticky top-0 z-50 border-b border-[#6f88b4]/15 bg-card/80 backdrop-blur-xs">
      <div className="mx-auto flex h-14 max-w-(--breakpoint-2xl) items-center px-4">
        <div className="flex w-full items-center justify-between">
          {/* Left side - primary nav */}
          <div className="flex items-center gap-4">
            <NavLink href="/dashboard">
              <Image
                src="/oddish.png"
                alt="Oddish"
                width={24}
                height={24}
                className="drop-shadow-xs"
              />
              <span>Dashboard</span>
            </NavLink>
            <NavLink href="/tasks">
              <FileText className="h-4 w-4" />
              <span>Tasks</span>
            </NavLink>
          </div>

          {/* Right side - consolidated settings menu */}
          <div className="flex items-center gap-2">
            <ThemeToggle />
            <Show when="signed-in">
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
              <UserMenu
                imageUrl={user?.imageUrl ?? null}
                firstName={user?.firstName ?? null}
                fullName={user?.fullName ?? null}
                username={user?.username ?? null}
                email={user?.primaryEmailAddress?.emailAddress ?? null}
              />
            </Show>
            <Show when="signed-out">
              <SignInButton mode="modal" fallbackRedirectUrl="/dashboard">
                <Button variant="outline" size="sm">
                  Sign in
                </Button>
              </SignInButton>
            </Show>
          </div>
        </div>
      </div>
    </nav>
  );
}
