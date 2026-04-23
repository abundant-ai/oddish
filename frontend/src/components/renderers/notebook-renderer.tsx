"use client";

import { useMemo } from "react";
import { MarkdownRenderer } from "./markdown-renderer";
import { CodeHighlight } from "./code-highlight";

interface NotebookRendererProps {
  content: string;
}

interface NotebookCell {
  cell_type: "code" | "markdown" | "raw";
  source: string[] | string;
  outputs?: CellOutput[];
  execution_count?: number | null;
  metadata?: Record<string, unknown>;
}

interface CellOutput {
  output_type: "stream" | "execute_result" | "display_data" | "error";
  text?: string[] | string;
  data?: Record<string, string[] | string>;
  name?: string;
  ename?: string;
  evalue?: string;
  traceback?: string[];
  execution_count?: number | null;
}

interface Notebook {
  cells: NotebookCell[];
  metadata?: {
    kernelspec?: { language?: string; display_name?: string };
    language_info?: { name?: string };
  };
}

function joinSource(source: string[] | string): string {
  return Array.isArray(source) ? source.join("") : source;
}

function stripAnsi(text: string): string {
  // eslint-disable-next-line no-control-regex
  return text.replace(/\x1b\[[0-9;]*m/g, "");
}

function getLanguage(notebook: Notebook): string {
  return (
    notebook.metadata?.language_info?.name ||
    notebook.metadata?.kernelspec?.language ||
    "python"
  );
}

function CellOutputDisplay({ output }: { output: CellOutput }) {
  switch (output.output_type) {
    case "stream": {
      const text = joinSource(output.text || "");
      return (
        <pre className="whitespace-pre-wrap bg-muted/30 px-4 py-2 font-mono text-xs text-foreground/80">
          {text}
        </pre>
      );
    }

    case "execute_result":
    case "display_data": {
      const data = output.data;
      if (!data) return null;

      const pngData = joinSource(data["image/png"] || "");
      if (pngData) {
        return (
          <div className="px-4 py-2">
            <img
              src={`data:image/png;base64,${pngData.trim()}`}
              alt="Cell output"
              className="h-auto max-w-full"
            />
          </div>
        );
      }

      const jpegData = joinSource(data["image/jpeg"] || "");
      if (jpegData) {
        return (
          <div className="px-4 py-2">
            <img
              src={`data:image/jpeg;base64,${jpegData.trim()}`}
              alt="Cell output"
              className="h-auto max-w-full"
            />
          </div>
        );
      }

      const svgData = joinSource(data["image/svg+xml"] || "");
      if (svgData) {
        return (
          <div className="px-4 py-2">
            <img
              src={`data:image/svg+xml;base64,${btoa(unescape(encodeURIComponent(svgData)))}`}
              alt="Cell output"
              className="h-auto max-w-full"
            />
          </div>
        );
      }

      const textData = joinSource(data["text/plain"] || "");
      if (textData) {
        return (
          <pre className="whitespace-pre-wrap bg-muted/30 px-4 py-2 font-mono text-xs text-foreground/80">
            {textData}
          </pre>
        );
      }

      return null;
    }

    case "error": {
      const traceback = (output.traceback || []).map(stripAnsi).join("\n");
      return (
        <pre className="whitespace-pre-wrap bg-red-500/5 px-4 py-2 font-mono text-xs text-red-500">
          {traceback || `${output.ename}: ${output.evalue}`}
        </pre>
      );
    }

    default:
      return null;
  }
}

function CodeCell({
  cell,
  language,
}: {
  cell: NotebookCell;
  language: string;
}) {
  const source = joinSource(cell.source);
  const execCount = cell.execution_count;
  const outputs = cell.outputs || [];

  return (
    <div className="overflow-hidden rounded-md border border-border">
      <div className="flex items-start bg-muted/20">
        <div className="w-14 shrink-0 select-none border-r border-border/50 py-2 pr-2 text-right font-mono text-xs text-muted-foreground/60">
          [{execCount ?? " "}]
        </div>
        <div className="min-w-0 flex-1 overflow-x-auto">
          <CodeHighlight
            code={source}
            language={language}
            showLineNumbers={false}
          />
        </div>
      </div>
      {outputs.length > 0 && (
        <div className="border-t border-border/50">
          {outputs.map((output, i) => (
            <CellOutputDisplay key={i} output={output} />
          ))}
        </div>
      )}
    </div>
  );
}

function RawCell({ cell }: { cell: NotebookCell }) {
  return (
    <pre className="whitespace-pre-wrap rounded-md border border-border bg-muted/20 px-4 py-3 font-mono text-xs text-foreground/70">
      {joinSource(cell.source)}
    </pre>
  );
}

export function NotebookRenderer({ content }: NotebookRendererProps) {
  const { notebook, error } = useMemo(() => {
    try {
      const parsed = JSON.parse(content) as Notebook;
      if (!parsed.cells || !Array.isArray(parsed.cells)) {
        return {
          notebook: null,
          error: "Invalid notebook: missing cells array",
        };
      }
      return { notebook: parsed, error: null };
    } catch {
      return { notebook: null, error: "Failed to parse notebook JSON" };
    }
  }, [content]);

  if (error || !notebook) {
    return <div className="p-6 text-sm text-destructive">{error}</div>;
  }

  const language = getLanguage(notebook);

  return (
    <div className="space-y-2 p-3">
      {notebook.cells.map((cell, i) => {
        switch (cell.cell_type) {
          case "markdown":
            return (
              <div key={i}>
                <MarkdownRenderer content={joinSource(cell.source)} />
              </div>
            );
          case "code":
            return <CodeCell key={i} cell={cell} language={language} />;
          case "raw":
            return <RawCell key={i} cell={cell} />;
          default:
            return null;
        }
      })}
    </div>
  );
}
