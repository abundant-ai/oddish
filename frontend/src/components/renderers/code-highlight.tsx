"use client";

import { Highlight, themes } from "prism-react-renderer";
import { useIsDark } from "./use-is-dark";

interface CodeHighlightProps {
  code: string;
  language: string;
  showLineNumbers?: boolean;
}

const LANGUAGE_MAP: Record<string, string> = {
  ts: "typescript",
  tsx: "tsx",
  js: "javascript",
  jsx: "jsx",
  json: "json",
  py: "python",
  txt: "text",
  md: "markdown",
  sh: "bash",
  bash: "bash",
  zsh: "bash",
  yml: "yaml",
  yaml: "yaml",
  docker: "docker",
  dockerfile: "docker",
  toml: "toml",
  ini: "ini",
  cfg: "ini",
  conf: "ini",
  xml: "xml",
  html: "html",
  css: "css",
  scss: "scss",
  sql: "sql",
  go: "go",
  rs: "rust",
  rust: "rust",
  c: "c",
  cpp: "cpp",
  h: "c",
  hpp: "cpp",
  java: "java",
  rb: "ruby",
  ruby: "ruby",
  php: "php",
  swift: "swift",
  kt: "kotlin",
  scala: "scala",
  r: "r",
  diff: "diff",
  patch: "diff",
  makefile: "makefile",
  make: "makefile",
  graphql: "graphql",
  gql: "graphql",
};

export function CodeHighlight({
  code,
  language,
  showLineNumbers = true,
}: CodeHighlightProps) {
  const isDark = useIsDark();
  const prismLanguage =
    LANGUAGE_MAP[language.toLowerCase()] || language || "text";

  return (
    <Highlight
      theme={isDark ? themes.nightOwl : themes.github}
      code={code.trimEnd()}
      language={prismLanguage}
    >
      {({ className, style, tokens, getLineProps, getTokenProps }) => (
        <pre
          className={className}
          style={{
            ...style,
            margin: 0,
            padding: "0.75rem",
            backgroundColor: "transparent",
            fontSize: "0.75rem",
            lineHeight: "1.5",
            fontFamily:
              "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, monospace",
          }}
        >
          {tokens.map((line, i) => {
            const lineProps = getLineProps({ line });
            return (
              <div
                key={i}
                {...lineProps}
                style={{ ...lineProps.style, minHeight: "1.5em" }}
              >
                {showLineNumbers && (
                  <span className="mr-4 inline-block w-8 select-none text-right text-xs text-muted-foreground/50">
                    {i + 1}
                  </span>
                )}
                {line.map((token, key) => (
                  <span key={key} {...getTokenProps({ token })} />
                ))}
              </div>
            );
          })}
        </pre>
      )}
    </Highlight>
  );
}
