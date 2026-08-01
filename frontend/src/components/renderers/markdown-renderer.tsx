"use client";

import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkBreaks from "remark-breaks";
import { Highlight, themes } from "prism-react-renderer";
import { Check, Copy } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useIsDark } from "./use-is-dark";
import {
  lineRangesIntersect,
  type LineRange,
} from "@/lib/line-range";

interface MarkdownRendererProps {
  content: string;
  /**
   * Source-line range to highlight (the ``?lines=L12-L20`` anchor). The
   * grammar is source lines of the .md file — the same address the raw
   * view uses — mapped onto rendered blocks via remark position data.
   */
  selectedLines?: LineRange | null;
  onSelectLines?: (range: LineRange | null) => void;
}

/** Position payload remark attaches to every node it parsed from source. */
interface MdNode {
  position?: {
    start?: { line?: number; column?: number };
    end?: { line?: number; column?: number };
  };
  properties?: Record<string, unknown>;
}

function nodeLineRange(node: MdNode | undefined): LineRange | null {
  const start = node?.position?.start?.line;
  if (!start) return null;
  let end = node?.position?.end?.line ?? start;
  // unist end points are exclusive: an end at column 1 means the block
  // ended with the previous line's newline (common for lists), so the
  // block does not actually cover this line. Treating it as covered made
  // adjacent blocks overlap by one line.
  if (end > start && node?.position?.end?.column === 1) end -= 1;
  return { start, end: Math.max(start, end) };
}

/**
 * Marks the document root's direct children so ``isRootBlock`` can tell
 * them apart from nested content at render time (hast nodes carry no
 * parent pointers). A column-1 heuristic breaks on CommonMark's optional
 * 1-3 space indent, which would leave validly indented root blocks
 * without anchors.
 */
type HastChild = MdNode & {
  type?: string;
  tagName?: string;
  children?: HastChild[];
};

function rehypeMarkRootBlocks() {
  return (tree: { children?: HastChild[] }) => {
    for (const child of tree.children ?? []) {
      if (child.type !== "element") continue;
      child.properties = { ...child.properties, dataMdRoot: "" };
      // Fenced code anchors on the code element nested inside the root
      // pre (the pre component just unwraps to its children), so the
      // mark has to reach through.
      if (child.tagName === "pre") {
        for (const nested of child.children ?? []) {
          if (nested.type === "element" && nested.tagName === "code") {
            nested.properties = { ...nested.properties, dataMdRoot: "" };
          }
        }
      }
    }
  };
}

/**
 * Only root-level blocks get anchors; nested content (list items' bodies,
 * blockquote children) is covered by its parent's range.
 */
function isRootBlock(node: MdNode | undefined): boolean {
  return node?.properties?.dataMdRoot !== undefined;
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
        <Button
          type="button"
          variant="ghost"
          onClick={handleCopy}
          className="h-auto gap-1 bg-transparent p-0 text-[11px] font-normal text-muted-foreground transition-colors hover:bg-transparent hover:text-foreground"
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
        </Button>
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

export function MarkdownRenderer({
  content,
  selectedLines,
  onSelectLines,
}: MarkdownRendererProps) {
  const anchorsEnabled = onSelectLines !== undefined || selectedLines != null;
  const containerRef = useRef<HTMLDivElement>(null);

  // Scroll the deep-linked range into view once: find the first block whose
  // source range intersects the selection. Later clicks must not yank the
  // viewport — the user is already there.
  const didScrollRef = useRef(false);
  // A new document re-arms the once-only scroll, so a deep-linked range in
  // the next file scrolls into view too.
  useEffect(() => {
    didScrollRef.current = false;
  }, [content]);
  useEffect(() => {
    if (didScrollRef.current || !selectedLines) return;
    didScrollRef.current = true;
    const container = containerRef.current;
    if (!container) return;
    const blocks = Array.from(
      container.querySelectorAll<HTMLElement>("[data-md-start]")
    );
    const target = blocks.find((el) =>
      lineRangesIntersect(selectedLines, {
        start: Number(el.dataset.mdStart),
        end: Number(el.dataset.mdEnd),
      })
    );
    target?.scrollIntoView({ block: "center" });
    // ``content`` is a dependency so the scroll re-fires after the re-arm
    // above when navigating documents with the same deep-linked range.
  }, [selectedLines, content]);

  const handleBlockClick = (range: LineRange, shiftKey: boolean) => {
    if (!onSelectLines) return;
    // A user-driven selection means the user is already looking at the
    // block — disarm the deep-link scroll so it never yanks the view.
    didScrollRef.current = true;
    if (shiftKey && selectedLines) {
      // Shift-click merges the block into the selection.
      onSelectLines({
        start: Math.min(selectedLines.start, range.start),
        end: Math.max(selectedLines.end, range.end),
      });
      return;
    }
    // Plain click selects the block; clicking a block that already shows
    // as selected clears. The clear test matches the highlight test
    // (intersection, not equality) so a deep-linked partial range inside
    // a block clears on the first click instead of re-selecting.
    onSelectLines(
      lineRangesIntersect(selectedLines ?? null, range) ? null : range
    );
  };

  // Wrap a root-level block so it carries its source-line range: a faint
  // line number in the left gutter (hover-revealed) selects the block, and
  // the block highlights when the selection intersects it. Non-root nodes
  // and renderers without the feature wired render unchanged.
  const anchored = (node: MdNode | undefined, element: React.ReactNode) => {
    const range = anchorsEnabled ? nodeLineRange(node) : null;
    if (!range || !isRootBlock(node)) return <>{element}</>;
    const selected = lineRangesIntersect(selectedLines ?? null, range);
    return (
      <div
        // The wrapper takes over first/last-child duty from the block
        // elements' own first:mt-0 / last:mb-0 (they're no longer direct
        // children of the body): the leading gutter button makes every
        // wrapped element :last-child, so without the higher-specificity
        // [&:not(:last-child)>p] rule every root paragraph would match its
        // own last:mb-0 and inter-paragraph spacing would collapse.
        data-md-start={range.start}
        data-md-end={range.end}
        // The first-child rule replaces the heading elements' own
        // first:mt-0 (they're no longer direct children of the body); it
        // targets headings only so leading blockquotes/code keep their
        // normal top margin, same as non-anchored markdown.
        className={`group/md-block relative [&:first-child>:is(h1,h2,h3,h4,h5,h6)]:mt-0 [&:not(:last-child)>p]:mb-3 ${
          selected ? "rounded bg-amber-400/15 dark:bg-amber-300/10" : ""
        }`}
      >
        {onSelectLines ? (
          // The gutter button only renders when selection is wired —
          // highlight-only usage (selectedLines without onSelectLines)
          // must not show controls that do nothing.
          <button
            type="button"
            onClick={(event) => handleBlockClick(range, event.shiftKey)}
            onMouseDown={(event) => {
              if (event.shiftKey) event.preventDefault();
            }}
            className={`absolute -left-8 top-0.5 w-7 select-none pr-1 text-right font-mono text-[10px] leading-6 transition-opacity hover:text-foreground focus-visible:opacity-100 ${
              selected
                ? "text-amber-700 opacity-100 dark:text-amber-300"
                : "text-muted-foreground/50 opacity-0 group-hover/md-block:opacity-100"
            }`}
            title={`Select lines ${range.start}${range.end !== range.start ? `-${range.end}` : ""}`}
            aria-label={`Select source lines ${range.start} to ${range.end}`}
          >
            {range.start}
          </button>
        ) : null}
        {element}
      </div>
    );
  };

  return (
    <div
      ref={containerRef}
      className={`markdown-body max-w-none p-4 text-sm ${anchorsEnabled ? "pl-10" : ""}`}
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkBreaks]}
        rehypePlugins={[rehypeMarkRootBlocks]}
        components={{
          h1: ({ node, children }) =>
            anchored(
              node,
              <h1 className="mb-2 mt-5 border-b border-border pb-1 text-xl font-semibold text-foreground first:mt-0">
                {children}
              </h1>
            ),
          h2: ({ node, children }) =>
            anchored(
              node,
              <h2 className="mb-2 mt-5 border-b border-border/50 pb-1 text-lg font-semibold text-foreground first:mt-0">
                {children}
              </h2>
            ),
          h3: ({ node, children }) =>
            anchored(
              node,
              <h3 className="mb-1.5 mt-4 text-base font-semibold text-foreground first:mt-0">
                {children}
              </h3>
            ),
          h4: ({ node, children }) =>
            anchored(
              node,
              <h4 className="mb-1.5 mt-3 text-sm font-semibold text-foreground first:mt-0">
                {children}
              </h4>
            ),
          h5: ({ node, children }) =>
            anchored(
              node,
              <h5 className="mb-1.5 mt-3 text-xs font-semibold text-foreground first:mt-0">
                {children}
              </h5>
            ),
          h6: ({ node, children }) =>
            anchored(
              node,
              <h6 className="mb-1.5 mt-3 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground first:mt-0">
                {children}
              </h6>
            ),
          p: ({ node, children }) =>
            anchored(
              node,
              <p className="mb-3 leading-6 text-foreground/90 last:mb-0">
                {children}
              </p>
            ),
          a: ({ href, children }) => (
            <a
              href={href}
              target={href?.startsWith("http") ? "_blank" : undefined}
              rel={href?.startsWith("http") ? "noopener noreferrer" : undefined}
              className="font-medium text-blue-600 underline-offset-4 hover:underline dark:text-blue-400"
            >
              {children}
            </a>
          ),
          ul: ({ node, children }) =>
            anchored(
              node,
              <ul className="mb-3 ml-5 list-outside list-disc space-y-1 text-foreground/90">
                {children}
              </ul>
            ),
          ol: ({ node, children }) =>
            anchored(
              node,
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
                <Checkbox
                  checked={checked}
                  disabled={disabled}
                  className="mt-1 h-3.5 w-3.5 border-border"
                />
              );
            }
            return (
              <Input
                type={type}
                checked={checked}
                disabled={disabled}
                readOnly
              />
            );
          },
          blockquote: ({ node, children }) =>
            anchored(
              node,
              <blockquote className="my-3 rounded-r border-l-2 border-primary/50 bg-muted/20 py-0.5 pl-3 italic text-muted-foreground">
                {children}
              </blockquote>
            ),
          hr: ({ node }) =>
            anchored(node, <hr className="my-6 border-border" />),
          code: ({ node, className, children }) => {
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

            return anchored(
              node,
              <CodeBlock language={match?.[1] || ""}>
                {String(children)}
              </CodeBlock>
            );
          },
          pre: ({ children }) => <>{children}</>,
          table: ({ node, children }) =>
            anchored(
              node,
              <Table className="my-3 rounded border border-border text-xs">
                {children}
              </Table>
            ),
          thead: ({ children }) => (
            <TableHeader className="border-b border-border bg-muted/50">
              {children}
            </TableHeader>
          ),
          tbody: ({ children }) => (
            <TableBody className="divide-y divide-border">{children}</TableBody>
          ),
          tr: ({ children }) => (
            <TableRow className="transition-colors hover:bg-muted/30">
              {children}
            </TableRow>
          ),
          th: ({ children }) => (
            <TableHead className="px-3 py-2 text-left font-semibold text-foreground">
              {children}
            </TableHead>
          ),
          td: ({ children }) => (
            <TableCell className="px-3 py-2 text-foreground/90">
              {children}
            </TableCell>
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
