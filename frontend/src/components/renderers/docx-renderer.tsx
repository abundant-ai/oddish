"use client";

import { useState, useEffect, useMemo } from "react";
import { Loader2 } from "lucide-react";

interface DocxRendererProps {
  data: ArrayBuffer;
  fileName: string;
}

/** Strip all HTML tags/attributes except a safe allowlist. */
function sanitizeHtml(html: string): string {
  const ALLOWED_TAGS = new Set([
    "p",
    "br",
    "b",
    "strong",
    "i",
    "em",
    "u",
    "s",
    "del",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "ul",
    "ol",
    "li",
    "table",
    "thead",
    "tbody",
    "tfoot",
    "tr",
    "th",
    "td",
    "blockquote",
    "pre",
    "code",
    "hr",
    "sup",
    "sub",
    "span",
    "a",
    "img",
  ]);

  const ALLOWED_ATTRS: Record<string, Set<string>> = {
    a: new Set(["href"]),
    img: new Set(["src", "alt", "width", "height"]),
    td: new Set(["colspan", "rowspan"]),
    th: new Set(["colspan", "rowspan"]),
  };

  const doc = new DOMParser().parseFromString(html, "text/html");

  function walk(node: Node): string {
    if (node.nodeType === Node.TEXT_NODE) {
      return escapeText(node.textContent ?? "");
    }

    if (node.nodeType !== Node.ELEMENT_NODE) return "";

    const el = node as Element;
    const tag = el.tagName.toLowerCase();

    if (!ALLOWED_TAGS.has(tag)) {
      return Array.from(el.childNodes).map(walk).join("");
    }

    const allowedAttrs = ALLOWED_ATTRS[tag];
    let attrs = "";
    if (allowedAttrs) {
      for (const attr of Array.from(el.attributes)) {
        if (!allowedAttrs.has(attr.name.toLowerCase())) continue;
        const val = attr.value;
        if (
          (attr.name === "href" || attr.name === "src") &&
          /^\s*javascript\s*:/i.test(val)
        ) {
          continue;
        }
        attrs += ` ${attr.name}="${escapeAttr(val)}"`;
      }
    }

    if (tag === "a") {
      attrs += ' target="_blank" rel="noopener noreferrer"';
    }

    const children = Array.from(el.childNodes).map(walk).join("");
    const selfClosing = ["br", "hr", "img"].includes(tag);
    return selfClosing
      ? `<${tag}${attrs} />`
      : `<${tag}${attrs}>${children}</${tag}>`;
  }

  return Array.from(doc.body.childNodes).map(walk).join("");
}

function escapeText(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function escapeAttr(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

export function DocxRenderer({ data, fileName }: DocxRendererProps) {
  const [rawHtml, setRawHtml] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<number>(0);

  useEffect(() => {
    let cancelled = false;
    import("mammoth")
      .then((mammoth) => mammoth.convertToHtml({ arrayBuffer: data }))
      .then((result) => {
        if (cancelled) return;
        setRawHtml(result.value);
        setWarnings(result.messages.length);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const message =
          err instanceof Error ? err.message : "Failed to parse document";
        setError(message);
      });
    return () => {
      cancelled = true;
    };
  }, [data]);

  const safeHtml = useMemo(() => {
    if (rawHtml === null) return null;
    return sanitizeHtml(rawHtml);
  }, [rawHtml]);

  if (error) {
    return (
      <div className="p-4 text-destructive">
        Failed to render {fileName}: {error}
      </div>
    );
  }

  if (safeHtml === null) {
    return (
      <div className="flex h-full items-center justify-center gap-2 p-8 text-muted-foreground">
        <Loader2 className="h-5 w-5 animate-spin" />
        <span>Converting document...</span>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      <div
        className="prose prose-sm max-w-none overflow-auto p-6 dark:prose-invert"
        dangerouslySetInnerHTML={{ __html: safeHtml }}
      />
      {warnings > 0 && (
        <div className="px-4 pb-3 text-xs text-muted-foreground">
          {warnings} conversion warning{warnings !== 1 ? "s" : ""}
        </div>
      )}
    </div>
  );
}
