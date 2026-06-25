"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Button } from "@/components/ui/button";

const CONFIG_TABS = [
  { href: "/qa/skills", label: "Skills" },
  { href: "/qa/documents", label: "Documents" },
];

export default function QaLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const runProbeActive = pathname === "/qa/run";
  const runsActive = pathname.startsWith("/qa/runs");

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-4 border-b border-[#6f88b4]/15 pb-3">
        <Button
          variant={runProbeActive ? "secondary" : "ghost"}
          size="sm"
          asChild
          className="border border-transparent data-[active=true]:border-[#85b85c]/25"
        >
          <Link href="/qa/run" data-active={runProbeActive}>
            Run Probe
          </Link>
        </Button>

        <Button
          variant={runsActive ? "secondary" : "ghost"}
          size="sm"
          asChild
          className="border border-transparent data-[active=true]:border-[#85b85c]/25"
        >
          <Link href="/qa/runs" data-active={runsActive}>
            Probe Runs
          </Link>
        </Button>

        {CONFIG_TABS.map((t) => {
          const active = pathname.startsWith(t.href);
          return (
            <Button
              key={t.href}
              variant={active ? "secondary" : "ghost"}
              size="sm"
              asChild
              className="border border-transparent data-[active=true]:border-[#85b85c]/25"
            >
              <Link href={t.href} data-active={active}>
                {t.label}
              </Link>
            </Button>
          );
        })}
      </div>

      {children}
    </div>
  );
}
