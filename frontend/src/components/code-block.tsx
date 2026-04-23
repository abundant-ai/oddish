"use client";

import { useState, useEffect, useMemo, useCallback } from "react";
import { Copy, Check } from "lucide-react";

let shikiPromise: Promise<typeof import("shiki")> | null = null;
const HIGHLIGHT_MAX_CHARS = 20_000;

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
  const shouldHighlight = useMemo(
    () => language !== "text" && truncatedCode.length <= HIGHLIGHT_MAX_CHARS,
    [language, truncatedCode],
  );

  useEffect(() => {
    if (!shouldHighlight) {
      setHighlightedHtml(null);
      return;
    }

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
  }, [truncatedCode, language, shouldHighlight]);

  const handleCopy = useCallback(async () => {
    await navigator.clipboard.writeText(code);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, [code]);

  const heightStyle = maxHeight === "none" ? { height: "100%" } : { maxHeight };

  return (
    <div className={`group relative ${className || ""}`}>
      {showCopyButton && (
        <button
          type="button"
          onClick={handleCopy}
          className="absolute right-1.5 top-1.5 z-10 inline-flex h-6 w-6 items-center justify-center rounded bg-muted/80 opacity-0 transition-opacity hover:bg-muted group-hover:opacity-100"
          title="Copy to clipboard"
          aria-label="Copy to clipboard"
        >
          {copied ? (
            <Check className="h-3 w-3 text-green-600 dark:text-green-400" />
          ) : (
            <Copy className="h-3 w-3 text-muted-foreground" />
          )}
        </button>
      )}
      {highlightedHtml ? (
        <div
          className="overflow-x-auto overflow-y-auto rounded border border-border text-xs [&>pre]:m-0 [&>pre]:overflow-x-auto [&>pre]:whitespace-pre-wrap [&>pre]:wrap-break-word [&>pre]:p-3"
          style={heightStyle}
          dangerouslySetInnerHTML={{ __html: highlightedHtml }}
        />
      ) : (
        <pre
          className="overflow-x-auto overflow-y-auto whitespace-pre-wrap wrap-break-word rounded border border-border bg-muted/50 p-3 text-xs text-foreground"
          style={heightStyle}
        >
          {truncatedCode}
        </pre>
      )}
    </div>
  );
}
