import { Button } from "@/components/ui/button";
import { GithubIcon } from "@/components/icons/github";

export function Footer() {
  return (
    <footer className="w-full border-t border-[#6f88b4]/15 px-6 py-3 dark:border-[#85b85c]/10">
      <div className="mx-auto flex max-w-5xl items-center justify-center gap-3 text-sm text-muted-foreground">
        <span>
          by{" "}
          <a
            href="https://abundantdata.com/"
            target="_blank"
            rel="noopener noreferrer"
            className="transition-colors hover:text-foreground"
          >
            Abundant AI
          </a>
        </span>
        <Button
          variant="ghost"
          size="icon"
          asChild
          className="h-8 w-8 rounded-full border border-[#6f88b4]/30 bg-background/60 text-foreground hover:border-emerald-600/35 hover:bg-emerald-500/10 hover:text-foreground"
        >
          <a
            href="https://github.com/abundant-ai/oddish"
            target="_blank"
            rel="noopener noreferrer"
            aria-label="Oddish GitHub"
          >
            <GithubIcon className="h-4 w-4" />
          </a>
        </Button>
      </div>
    </footer>
  );
}
