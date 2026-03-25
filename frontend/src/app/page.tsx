"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { SignedIn, SignedOut, SignUpButton } from "@clerk/nextjs";
import { ArrowRight, Github } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ThemeToggle } from "@/components/theme-toggle";
import Image from "next/image";

function RedirectToDashboard() {
  const router = useRouter();

  useEffect(() => {
    router.replace("/dashboard");
  }, [router]);

  return (
    <div className="flex min-h-screen items-center justify-center bg-background">
      <p className="text-muted-foreground">Redirecting to dashboard...</p>
    </div>
  );
}

export default function LandingPage() {
  const command = "oddish run -d terminal-bench@2.0 -c sweep.yaml";
  const [typedCommand, setTypedCommand] = useState("");
  const [cursorVisible, setCursorVisible] = useState(true);

  useEffect(() => {
    const intervalId = setInterval(() => {
      setCursorVisible((visible) => !visible);
    }, 500);

    return () => {
      clearInterval(intervalId);
    };
  }, []);

  useEffect(() => {
    let index = 0;
    let timeoutId: ReturnType<typeof setTimeout> | undefined;

    const startTyping = () => {
      index = 0;
      const typeNext = () => {
        setTypedCommand(command.slice(0, index));
        if (index < command.length) {
          index += 1;
          timeoutId = setTimeout(typeNext, 120);
        } else {
          timeoutId = setTimeout(startDeleting, 15000);
        }
      };
      typeNext();
    };

    const startDeleting = () => {
      index = command.length;
      const deleteNext = () => {
        setTypedCommand(command.slice(0, index));
        if (index > 0) {
          index -= 1;
          timeoutId = setTimeout(deleteNext, 60);
        } else {
          timeoutId = setTimeout(startTyping, 400);
        }
      };
      deleteNext();
    };

    setTypedCommand("");
    startTyping();

    return () => {
      if (timeoutId) {
        clearTimeout(timeoutId);
      }
    };
  }, [command]);

  return (
    <>
      <SignedIn>
        <RedirectToDashboard />
      </SignedIn>

      <SignedOut>
        <div className="flex min-h-screen flex-col overflow-hidden bg-[radial-gradient(circle_at_top,rgba(133,184,92,0.16),transparent_34%),radial-gradient(circle_at_80%_20%,rgba(111,136,180,0.12),transparent_28%),linear-gradient(to_bottom,hsl(var(--background)),hsl(var(--background)))] text-foreground">
          {/* Header */}
          <header className="w-full border-b border-emerald-700/15 px-6 py-3 dark:border-emerald-400/10">
            <div className="mx-auto flex max-w-5xl items-center justify-between">
              <div className="flex items-center gap-2">
                <Image
                  src="/oddish.png"
                  alt="Oddish"
                  width={32}
                  height={32}
                  className="drop-shadow-sm"
                />
                <span className="text-lg font-semibold">Oddish</span>
              </div>
              <div className="flex items-center gap-3">
                <ThemeToggle />
                <SignUpButton mode="modal" fallbackRedirectUrl="/dashboard">
                  <Button
                    variant="outline"
                    size="sm"
                    className="border-emerald-700/25 bg-background/70 shadow-sm hover:border-emerald-600/35 hover:bg-emerald-500/10 dark:border-emerald-400/20 dark:hover:border-emerald-300/30 dark:hover:bg-emerald-400/10"
                  >
                    Sign Up
                  </Button>
                </SignUpButton>
                <a
                  href="https://github.com/abundant-ai/oddish"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center justify-center rounded-full border border-[#6f88b4]/30 bg-background/60 p-2 text-[#5c7096] transition-colors hover:border-emerald-600/35 hover:bg-emerald-500/10 hover:text-foreground dark:text-[#9db0cf]"
                  aria-label="Oddish GitHub"
                >
                  <Github className="h-4 w-4" />
                </a>
              </div>
            </div>
          </header>

          {/* Main content */}
          <main className="relative flex flex-1 flex-col items-center justify-center px-6 py-8 sm:py-10">
            <div
              aria-hidden="true"
              className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_25%_0%,rgba(133,184,92,0.2),transparent_34%),radial-gradient(circle_at_72%_18%,rgba(111,136,180,0.14),transparent_30%),linear-gradient(to_bottom,rgba(0,0,0,0),rgba(0,0,0,0.08))]"
            />
            <div className="relative w-full max-w-5xl space-y-8 sm:space-y-10">
              {/* Hero */}
              <div className="grid items-center gap-6 md:grid-cols-[1.05fr_0.95fr]">
                <div className="text-center md:text-left">
                  <h1 className="text-4xl font-bold tracking-tight sm:text-5xl">
                    Run{" "}
                    <a
                      href="https://harborframework.com/"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="transition-colors hover:text-[#5d77a5] dark:hover:text-[#a8b8d2]"
                    >
                      Harbor
                    </a>{" "}
                    tasks
                    <span className="block text-[#5c8e43] dark:text-[#85b85c]">
                      on the cloud
                    </span>
                  </h1>
                </div>
                <div className="relative mx-auto w-full max-w-sm">
                  <div
                    aria-hidden="true"
                    className="absolute inset-0 rounded-full bg-[radial-gradient(circle,rgba(133,184,92,0.34),transparent_55%)] blur-3xl"
                  />
                  <Image
                    src="/oddish.png"
                    alt="Oddish pixel art"
                    width={512}
                    height={512}
                    priority
                    className="relative mx-auto h-auto w-full max-w-[16rem] drop-shadow-[0_16px_26px_rgba(73,96,137,0.24)]"
                  />
                </div>
              </div>

              {/* Terminal */}
              <div className="overflow-hidden rounded-xl border border-[#85b85c]/15 bg-zinc-900 shadow-[0_20px_56px_rgba(39,55,85,0.18)] ring-1 ring-[#6f88b4]/10">
                <div className="flex items-center gap-2 border-b border-zinc-700 bg-[linear-gradient(90deg,rgba(111,136,180,0.16),rgba(133,184,92,0.08)),rgba(39,39,42,0.92)] px-4 py-2.5">
                  <div className="h-3 w-3 rounded-full bg-[#d79088]" />
                  <div className="h-3 w-3 rounded-full bg-[#c9cf8a]" />
                  <div className="h-3 w-3 rounded-full bg-[#85b85c]" />
                </div>
                <pre className="overflow-x-auto p-4 font-mono text-sm leading-relaxed text-zinc-300 sm:p-5">
                  <code>
                    <span className="text-zinc-500"># Submit a job</span>
                    {"\n"}
                    <span className="text-[#85b85c]">$</span> oddish run -d
                    terminal-bench@2.0 -a codex -m gpt-5.2-codex --n-trials 3
                    {"\n\n"}
                    <span className="text-zinc-500">
                      # Or sweep multiple agents
                    </span>
                    {"\n"}
                    <span className="text-[#85b85c]">$</span>{" "}
                    <span>{typedCommand}</span>
                    <span
                      aria-hidden="true"
                      className={`ml-1 inline-block h-4 w-2 bg-[#6f88b4] align-middle ${
                        cursorVisible ? "opacity-100" : "opacity-0"
                      }`}
                    />
                    {"\n\n"}
                    <span className="text-zinc-500"># Monitor progress</span>
                    {"\n"}
                    <span className="text-[#85b85c]">$</span> oddish status
                  </code>
                </pre>
              </div>

              {/* CTA */}
              <div className="flex justify-center pt-1">
                <Button
                  asChild
                  size="lg"
                  className="inline-flex items-center gap-2 bg-[#6f88b4] px-8 text-white shadow-[0_10px_28px_rgba(73,96,137,0.24)] hover:bg-[#647daa]"
                >
                  <a href="/settings?tab=api-keys">
                    Get Started
                    <ArrowRight className="h-4 w-4" />
                  </a>
                </Button>
              </div>
            </div>
          </main>

          {/* Footer */}
          <footer className="w-full border-t border-[#6f88b4]/15 px-6 py-3 dark:border-[#85b85c]/10">
            <div className="mx-auto max-w-5xl text-center text-sm text-muted-foreground">
              by{" "}
              <a
                href="https://abundantdata.com/"
                target="_blank"
                rel="noopener noreferrer"
                className="transition-colors hover:text-foreground"
              >
                Abundant AI
              </a>
            </div>
          </footer>
        </div>
      </SignedOut>
    </>
  );
}
