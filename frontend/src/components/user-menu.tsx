"use client";

import Link from "next/link";
import { SignOutButton } from "@clerk/nextjs";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { ChevronDown, LogOut, Shield, User } from "lucide-react";

export interface UserMenuProps {
  imageUrl: string | null;
  firstName: string | null;
  fullName: string | null;
  username: string | null;
  email: string | null;
}

export function UserMenu({
  imageUrl,
  firstName,
  fullName,
  username,
  email,
}: UserMenuProps) {
  const displayName = firstName ?? fullName ?? "Account";
  const fallback = firstName?.[0] ?? "U";

  return (
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
              src={imageUrl ?? undefined}
              alt={fullName ?? "User avatar"}
            />
            <AvatarFallback className="text-xs font-semibold">
              {fallback}
            </AvatarFallback>
          </Avatar>
          <span className="hidden md:inline">{displayName}</span>
          <ChevronDown className="hidden h-4 w-4 text-muted-foreground sm:inline" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        className="w-64 border-[#6f88b4]/20 p-2"
      >
        <div className="px-2 py-1.5">
          <p className="text-sm font-medium">
            {fullName ?? username ?? "Account"}
          </p>
          <p className="text-xs text-muted-foreground">{email ?? "—"}</p>
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
        <SignOutButton>
          <DropdownMenuItem className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm text-red-500 outline-hidden hover:bg-muted focus:bg-muted">
            <LogOut className="h-4 w-4" />
            Sign out
          </DropdownMenuItem>
        </SignOutButton>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
