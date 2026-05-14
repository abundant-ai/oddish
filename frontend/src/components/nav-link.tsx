"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Button } from "@/components/ui/button";

export function NavLink({
  href,
  children,
}: {
  href: string;
  children: React.ReactNode;
}) {
  const pathname = usePathname();
  const active = pathname === href;
  return (
    <Button
      variant={active ? "secondary" : "ghost"}
      size="sm"
      asChild
      className="gap-2 border border-transparent data-[active=true]:border-[#85b85c]/25"
    >
      <Link
        href={href}
        className="flex items-center gap-2"
        data-active={active}
      >
        {children}
      </Link>
    </Button>
  );
}
