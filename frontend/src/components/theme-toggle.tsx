"use client";

import { useEffect, useState } from "react";
import { Moon, MoonStar, Sun } from "lucide-react";
import { Button } from "@/components/ui/button";

type Theme = "light" | "dark" | "midnight";

const STORAGE_KEY = "oddish-theme";

function applyTheme(theme: Theme) {
  if (typeof document === "undefined") {
    return;
  }
  // Midnight keeps the `dark` class so every dark-mode behavior (dark:
  // variants, shiki, Clerk overrides) still applies; `.midnight` only
  // re-skins the surface tokens on top.
  document.documentElement.classList.toggle("dark", theme !== "light");
  document.documentElement.classList.toggle("midnight", theme === "midnight");
}

function getInitialTheme(): Theme {
  if (typeof window === "undefined") {
    return "dark";
  }
  const stored = window.localStorage.getItem(STORAGE_KEY) as Theme | null;
  if (stored === "light" || stored === "dark" || stored === "midnight") {
    return stored;
  }
  const prefersDark = window.matchMedia?.(
    "(prefers-color-scheme: dark)",
  ).matches;
  return prefersDark ? "dark" : "light";
}

const NEXT_THEME: Record<Theme, Theme> = {
  light: "dark",
  dark: "midnight",
  midnight: "light",
};

export function ThemeToggle() {
  const [mounted, setMounted] = useState(false);
  const [theme, setTheme] = useState<Theme>("dark");

  useEffect(() => {
    const initial = getInitialTheme();
    setTheme(initial);
    applyTheme(initial);
    setMounted(true);
  }, []);

  useEffect(() => {
    if (!mounted) {
      return;
    }
    window.localStorage.setItem(STORAGE_KEY, theme);
    applyTheme(theme);
  }, [mounted, theme]);

  if (!mounted) {
    return null;
  }

  const nextTheme = NEXT_THEME[theme];
  const label = `Switch to ${nextTheme} mode`;

  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      aria-label={label}
      title={label}
      onClick={() => setTheme(nextTheme)}
    >
      {nextTheme === "light" ? (
        <Sun className="h-4 w-4" />
      ) : nextTheme === "dark" ? (
        <Moon className="h-4 w-4" />
      ) : (
        <MoonStar className="h-4 w-4" />
      )}
    </Button>
  );
}
