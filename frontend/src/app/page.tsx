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
    <div className="min-h-screen flex items-center justify-center bg-background">
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
        <div className="min-h-screen bg-background text-foreground flex flex-col">
          {/* Header */}
          <header className="w-full border-b border-border/50 px-6 py-4">
            <div className="max-w-5xl mx-auto flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Image
                  src="/oddish.jpg"
                  alt="Oddish"
                  width={32}
                  height={32}
                  className="drop-shadow-sm"
                />
                <span className="font-semibold text-lg">Oddish</span>
              </div>
              <div className="flex items-center gap-3">
                <Button variant="ghost" size="sm" asChild>
                  <a href="/datasets">Datasets</a>
                </Button>
                <ThemeToggle />
                <SignUpButton mode="modal" fallbackRedirectUrl="/dashboard">
                  <Button variant="outline" size="sm">
                    Sign Up
                  </Button>
                </SignUpButton>
                <a
                  href="https://github.com/abundant-ai/oddish"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center justify-center rounded-full border border-border/60 bg-muted/40 p-2 text-muted-foreground hover:text-foreground hover:border-border transition-colors"
                  aria-label="Oddish GitHub"
                >
                  <Github className="h-4 w-4" />
                </a>
              </div>
            </div>
          </header>

          {/* Main content */}
          <main className="flex-1 flex flex-col items-center justify-center px-6 py-12">
            <div className="max-w-3xl w-full space-y-12">
              {/* Hero */}
              <div className="text-center">
                <h1 className="text-4xl sm:text-5xl font-bold tracking-tight">
                  Run agent evals{" "}
                  <span className="text-primary/80">at scale</span>
                </h1>
              </div>

              {/* Terminal */}
              <div className="rounded-xl border border-border bg-zinc-900 shadow-lg overflow-hidden">
                <div className="flex items-center gap-2 px-4 py-3 bg-zinc-800/80 border-b border-zinc-700">
                  <div className="h-3 w-3 rounded-full bg-red-500/80" />
                  <div className="h-3 w-3 rounded-full bg-yellow-500/80" />
                  <div className="h-3 w-3 rounded-full bg-green-500/80" />
                </div>
                <pre className="p-5 text-sm text-zinc-300 overflow-x-auto font-mono leading-relaxed">
                  <code>
                    <span className="text-zinc-500"># Submit a job</span>
                    {"\n"}
                    <span className="text-green-400">$</span> oddish run -d
                    terminal-bench@2.0 -a codex -m gpt-5.2-codex --n-trials 3
                    {"\n\n"}
                    <span className="text-zinc-500">
                      # Or sweep multiple agents
                    </span>
                    {"\n"}
                    <span className="text-green-400">$</span>{" "}
                    <span>{typedCommand}</span>
                    <span
                      aria-hidden="true"
                      className={`ml-1 inline-block h-4 w-2 align-middle bg-zinc-300 ${
                        cursorVisible ? "opacity-100" : "opacity-0"
                      }`}
                    />
                    {"\n\n"}
                    <span className="text-zinc-500"># Monitor progress</span>
                    {"\n"}
                    <span className="text-green-400">$</span> oddish status
                  </code>
                </pre>
              </div>

              {/* CTA */}
              <div className="flex justify-center pt-4">
                <Button
                  asChild
                  size="lg"
                  className="inline-flex items-center gap-2 px-8"
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
          <footer className="w-full border-t border-border/50 px-6 py-4">
            <div className="max-w-5xl mx-auto text-center text-sm text-muted-foreground">
              by{" "}
              <a
                href="https://abundantdata.com/"
                target="_blank"
                rel="noopener noreferrer"
                className="hover:text-foreground transition-colors"
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
