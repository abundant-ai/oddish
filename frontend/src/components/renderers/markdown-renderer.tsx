"use client";

import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkBreaks from "remark-breaks";
import { Highlight, themes } from "prism-react-renderer";
import { Check, Copy } from "lucide-react";
import { useIsDark } from "./use-is-dark";

interface MarkdownRendererProps {
  content: string;
}

const LANGUAGE_MAP: Record<string, string> = {
  ts: "typescript",
  tsx: "tsx",
  js: "javascript",
  jsx: "jsx",
  json: "json",
  py: "python",
  python: "python",
  txt: "text",
  md: "markdown",
  sh: "bash",
  bash: "bash",
  shell: "bash",
  zsh: "bash",
  yml: "yaml",
  yaml: "yaml",
  docker: "docker",
  dockerfile: "docker",
  css: "css",
  scss: "css",
  html: "markup",
  xml: "markup",
  svg: "markup",
  sql: "sql",
  go: "go",
  rust: "rust",
  rs: "rust",
  java: "java",
  kotlin: "kotlin",
  kt: "kotlin",
  c: "c",
  cpp: "cpp",
  "c++": "cpp",
  cxx: "cpp",
  ruby: "ruby",
  rb: "ruby",
  diff: "diff",
  makefile: "makefile",
  make: "makefile",
  toml: "toml",
  ini: "ini",
  graphql: "graphql",
  gql: "graphql",
  swift: "swift",
  php: "php",
  r: "r",
  scala: "scala",
  haskell: "haskell",
  hs: "haskell",
  lua: "lua",
  perl: "perl",
  elixir: "elixir",
  ex: "elixir",
  clojure: "clojure",
  clj: "clojure",
};

function CodeBlock({
  language,
  children,
}: {
  language: string;
  children: string;
}) {
  const [copied, setCopied] = useState(false);
  const isDark = useIsDark();
  const prismLanguage = LANGUAGE_MAP[language] || language || "text";
  const code = String(children).replace(/\n$/, "");
  const displayLang = language || "text";

  const handleCopy = async () => {
    await navigator.clipboard.writeText(code);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="group relative my-3 overflow-hidden rounded-md border border-border bg-muted dark:bg-card">
      <div className="flex items-center justify-between border-b border-border/50 bg-muted/50 px-3 py-1.5 dark:bg-muted/30">
        <span className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
          {displayLang}
        </span>
        <button
          onClick={handleCopy}
          className="flex items-center gap-1 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
        >
          {copied ? (
            <>
              <Check className="h-3 w-3" />
              <span>Copied!</span>
            </>
          ) : (
            <>
              <Copy className="h-3 w-3" />
              <span>Copy</span>
            </>
          )}
        </button>
      </div>
      <Highlight
        theme={isDark ? themes.nightOwl : themes.github}
        code={code}
        language={prismLanguage}
      >
        {({ tokens, getLineProps, getTokenProps }) => (
          <pre className="overflow-x-auto p-3 text-xs leading-relaxed">
            {tokens.map((line, i) => (
              <div key={i} {...getLineProps({ line })} className="table-row">
                <span className="table-cell w-[3ch] select-none pr-3 text-right text-muted-foreground/40">
                  {i + 1}
                </span>
                <span className="table-cell">
                  {line.map((token, key) => (
                    <span key={key} {...getTokenProps({ token })} />
                  ))}
                </span>
              </div>
            ))}
          </pre>
        )}
      </Highlight>
    </div>
  );
}

export function MarkdownRenderer({ content }: MarkdownRendererProps) {
  return (
    <div className="markdown-body max-w-none p-4 text-sm">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkBreaks]}
        components={{
          h1: ({ children }) => (
            <h1 className="mb-2 mt-5 border-b border-border pb-1 text-xl font-semibold text-foreground first:mt-0">
              {children}
            </h1>
          ),
          h2: ({ children }) => (
            <h2 className="mb-2 mt-5 border-b border-border/50 pb-1 text-lg font-semibold text-foreground first:mt-0">
              {children}
            </h2>
          ),
          h3: ({ children }) => (
            <h3 className="mb-1.5 mt-4 text-base font-semibold text-foreground first:mt-0">
              {children}
            </h3>
          ),
          h4: ({ children }) => (
            <h4 className="mb-1.5 mt-3 text-sm font-semibold text-foreground first:mt-0">
              {children}
            </h4>
          ),
          h5: ({ children }) => (
            <h5 className="mb-1.5 mt-3 text-xs font-semibold text-foreground first:mt-0">
              {children}
            </h5>
          ),
          h6: ({ children }) => (
            <h6 className="mb-1.5 mt-3 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground first:mt-0">
              {children}
            </h6>
          ),
          p: ({ children }) => (
            <p className="mb-3 leading-6 text-foreground/90 last:mb-0">
              {children}
            </p>
          ),
          a: ({ href, children }) => (
            <a
              href={href}
              target={href?.startsWith("http") ? "_blank" : undefined}
              rel={href?.startsWith("http") ? "noopener noreferrer" : undefined}
              className="font-medium text-primary underline-offset-4 hover:underline"
            >
              {children}
            </a>
          ),
          ul: ({ children }) => (
            <ul className="mb-3 ml-5 list-outside list-disc space-y-1 text-foreground/90">
              {children}
            </ul>
          ),
          ol: ({ children }) => (
            <ol className="mb-3 ml-5 list-outside list-decimal space-y-1 text-foreground/90">
              {children}
            </ol>
          ),
          li: ({ children, className }) => {
            const isTaskItem = className?.includes("task-list-item");
            return (
              <li
                className={`leading-6 ${isTaskItem ? "-ml-5 flex list-none items-start gap-2" : ""}`}
              >
                {children}
              </li>
            );
          },
          input: ({ type, checked, disabled }) => {
            if (type === "checkbox") {
              return (
                <input
                  type="checkbox"
                  checked={checked}
                  disabled={disabled}
                  className="mt-1 h-3.5 w-3.5 rounded border-border accent-primary"
                  readOnly
                />
              );
            }
            return <input type={type} checked={checked} disabled={disabled} />;
          },
          blockquote: ({ children }) => (
            <blockquote className="my-3 rounded-r border-l-2 border-primary/50 bg-muted/20 py-0.5 pl-3 italic text-muted-foreground">
              {children}
            </blockquote>
          ),
          hr: () => <hr className="my-6 border-border" />,
          code: ({ className, children }) => {
            const match = /language-(\w+)/.exec(className || "");
            const isInline =
              !className &&
              typeof children === "string" &&
              !children.includes("\n");

            if (isInline) {
              return (
                <code className="rounded bg-muted px-1 py-0.5 font-mono text-[0.85em] text-primary">
                  {children}
                </code>
              );
            }

            return (
              <CodeBlock language={match?.[1] || ""}>
                {String(children)}
              </CodeBlock>
            );
          },
          pre: ({ children }) => <>{children}</>,
          table: ({ children }) => (
            <div className="my-3 overflow-x-auto rounded border border-border">
              <table className="w-full text-xs">{children}</table>
            </div>
          ),
          thead: ({ children }) => (
            <thead className="border-b border-border bg-muted/50">
              {children}
            </thead>
          ),
          tbody: ({ children }) => (
            <tbody className="divide-y divide-border">{children}</tbody>
          ),
          tr: ({ children }) => (
            <tr className="transition-colors hover:bg-muted/30">{children}</tr>
          ),
          th: ({ children }) => (
            <th className="px-3 py-2 text-left font-semibold text-foreground">
              {children}
            </th>
          ),
          td: ({ children }) => (
            <td className="px-3 py-2 text-foreground/90">{children}</td>
          ),
          img: ({ src, alt }) => (
            <span className="my-3 block">
              <img
                src={src as string | undefined}
                alt={alt || ""}
                className="h-auto max-w-full rounded border border-border shadow-xs"
              />
              {alt && (
                <span className="mt-1 block text-center text-xs italic text-muted-foreground">
                  {alt}
                </span>
              )}
            </span>
          ),
          strong: ({ children }) => (
            <strong className="font-semibold text-foreground">
              {children}
            </strong>
          ),
          em: ({ children }) => <em className="italic">{children}</em>,
          del: ({ children }) => (
            <del className="text-muted-foreground line-through">{children}</del>
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
