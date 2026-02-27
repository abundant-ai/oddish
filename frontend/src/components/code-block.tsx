"use client";

import { useState, useEffect, useMemo, useCallback } from "react";
import { Button } from "@/components/ui/button";
import { Copy, Check } from "lucide-react";

let shikiPromise: Promise<typeof import("shiki")> | null = null;

function getShiki() {
  if (!shikiPromise) {
    shikiPromise = import("shiki");
  }
  return shikiPromise;
}

export function getLanguageFromFilename(name: string): string {
  const ext = name.split(".").pop()?.toLowerCase();
  const langMap: Record<string, string> = {
    ts: "typescript",
    tsx: "typescript",
    js: "javascript",
    jsx: "javascript",
    py: "python",
    toml: "toml",
    yaml: "yaml",
    yml: "yaml",
    sh: "bash",
    bash: "bash",
    zsh: "bash",
    json: "json",
    md: "markdown",
    txt: "text",
    html: "html",
    htm: "html",
    css: "css",
    xml: "xml",
    sql: "sql",
    rs: "rust",
    go: "go",
    rb: "ruby",
    java: "java",
    c: "c",
    h: "c",
    cpp: "cpp",
    hpp: "cpp",
    cs: "csharp",
    dockerfile: "dockerfile",
    diff: "diff",
    patch: "diff",
    log: "text",
    cfg: "ini",
    ini: "ini",
    conf: "ini",
    env: "shell",
    csv: "text",
    r: "r",
    swift: "swift",
    kt: "kotlin",
    kts: "kotlin",
    lua: "lua",
    php: "php",
    pl: "perl",
    tex: "latex",
    makefile: "makefile",
  };
  if (!ext) {
    const lower = name.toLowerCase();
    if (lower === "dockerfile") return "dockerfile";
    if (lower === "makefile") return "makefile";
    return "text";
  }
  return langMap[ext] || "text";
}

interface CodeBlockProps {
  code: string;
  language?: string;
  className?: string;
  /** CSS max-height value. "none" disables the constraint (fills parent). Default: "16rem". */
  maxHeight?: string;
  /** Max character count before truncation. 0 disables truncation. Default: 50000. */
  truncateAt?: number;
  showCopyButton?: boolean;
}

export function CodeBlock({
  code,
  language = "text",
  className,
  maxHeight = "16rem",
  truncateAt = 50000,
  showCopyButton = true,
}: CodeBlockProps) {
  const [copied, setCopied] = useState(false);
  const [highlightedHtml, setHighlightedHtml] = useState<string | null>(null);

  const truncatedCode = useMemo(() => {
    if (truncateAt > 0 && code.length > truncateAt) {
      return code.slice(0, truncateAt) + "\n\n... (truncated)";
    }
    return code;
  }, [code, truncateAt]);

  useEffect(() => {
    let cancelled = false;

    async function highlight() {
      try {
        const shiki = await getShiki();
        const lang = language === "text" ? "text" : language;
        const html = await shiki.codeToHtml(truncatedCode, {
          lang,
          themes: {
            light: "github-light",
            dark: "github-dark-default",
          },
        });
        if (!cancelled) setHighlightedHtml(html);
      } catch {
        if (!cancelled) setHighlightedHtml(null);
      }
    }

    highlight();
    return () => {
      cancelled = true;
    };
  }, [truncatedCode, language]);

  const handleCopy = useCallback(async () => {
    await navigator.clipboard.writeText(code);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, [code]);

  const heightStyle =
    maxHeight === "none" ? { height: "100%" } : { maxHeight };

  return (
    <div className={`relative group ${className || ""}`}>
      {showCopyButton && (
        <Button
          type="button"
          variant="ghost"
          size="icon"
          onClick={handleCopy}
          className="absolute top-2 right-2 p-1.5 rounded bg-muted/80 hover:bg-muted opacity-0 group-hover:opacity-100 transition-opacity z-10"
          title="Copy to clipboard"
        >
          {copied ? (
            <Check className="h-3 w-3 text-green-600 dark:text-green-400" />
          ) : (
            <Copy className="h-3 w-3 text-muted-foreground" />
          )}
        </Button>
      )}
      {highlightedHtml ? (
        <div
          className="text-xs rounded border border-border overflow-x-auto overflow-y-auto [&>pre]:p-3 [&>pre]:m-0 [&>pre]:overflow-x-auto [&>pre]:whitespace-pre-wrap [&>pre]:break-words"
          style={heightStyle}
          dangerouslySetInnerHTML={{ __html: highlightedHtml }}
        />
      ) : (
        <pre
          className="text-xs bg-muted/50 text-foreground p-3 rounded border border-border overflow-x-auto overflow-y-auto whitespace-pre-wrap break-words"
          style={heightStyle}
        >
          {truncatedCode}
        </pre>
      )}
    </div>
  );
}
