import Image from "next/image";
import { ThemeToggle } from "@/components/theme-toggle";

export function ShareNav() {
  return (
    <nav className="bg-card/80 sticky top-[var(--preview-banner-h,0px)] z-40 border-b border-[#6f88b4]/15 backdrop-blur-xs">
      <div className="mx-auto flex h-14 max-w-(--breakpoint-2xl) items-center justify-between px-4">
        <div className="flex items-center gap-2">
          <Image
            src="/oddish.png"
            alt="Oddish"
            width={24}
            height={24}
            className="drop-shadow-xs"
          />
          <span className="text-sm font-semibold">Oddish</span>
          <span className="text-muted-foreground text-sm">by Abundant AI</span>
        </div>
        <ThemeToggle />
      </div>
    </nav>
  );
}
