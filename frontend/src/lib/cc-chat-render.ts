import type { ReactNode } from "react";
import { createElement as h } from "react";

export type RenderedEvent = {
  // The assistant's actual reply — shown inline in the transcript.
  primary: ReactNode | null;
  // Tool calls / results / system noise — tucked into a collapsed disclosure.
  secondary: ReactNode | null;
};

const EMPTY: RenderedEvent = { primary: null, secondary: null };

/**
 * Render a single Claude Code stream-json event into two buckets: the
 * `primary` reply text the user cares about, and `secondary` tool/system
 * activity that should hide behind a toggle.
 */
export function renderStreamEvent(event: unknown): RenderedEvent {
  if (!event || typeof event !== "object") {
    return {
      primary: null,
      secondary: h(
        "code",
        { className: "text-xs opacity-70" },
        String(event),
      ),
    };
  }

  const e = event as Record<string, unknown>;
  const type = String(e.type ?? "?");

  // Backend escape hatches first.
  if (type === "_stderr") {
    return {
      primary: null,
      secondary: h(
        "div",
        { className: "rounded bg-destructive/10 px-2 py-1 font-mono text-xs whitespace-pre-wrap text-destructive" },
        `stderr: ${String(e.text ?? "")}`,
      ),
    };
  }
  if (type === "_invalid_json") {
    return {
      primary: null,
      secondary: h(
        "div",
        { className: "rounded bg-muted px-2 py-1 font-mono text-xs opacity-70" },
        `invalid stream-json line: ${String(e.raw ?? "")}`,
      ),
    };
  }

  // Claude Code stream-json shapes:
  //   {type:"system", subtype:"init", session_id, ...}
  //   {type:"assistant", message:{content:[{type:"text",text:"..."},{type:"tool_use",name,input}]}}
  //   {type:"user", message:{content:[{type:"tool_result",content,is_error}]}}
  //   {type:"result", ...}
  if (type === "system") {
    if (e.subtype === "init") return EMPTY;
    return {
      primary: null,
      secondary: h(
        "div",
        { className: "text-xs opacity-60" },
        `system: ${String(e.subtype ?? "")}`,
      ),
    };
  }

  if (type === "assistant") {
    const message = e.message as Record<string, unknown> | undefined;
    const content = (message?.content ?? []) as Array<Record<string, unknown>>;
    const textBlocks: ReactNode[] = [];
    const toolBlocks: ReactNode[] = [];
    content.forEach((block, i) => {
      const btype = String(block.type ?? "");
      if (btype === "text") {
        textBlocks.push(
          h(
            "div",
            { key: `t${i}`, className: "whitespace-pre-wrap" },
            String(block.text ?? ""),
          ),
        );
      } else if (btype === "tool_use") {
        const name = String(block.name ?? "?");
        const input = block.input ?? {};
        toolBlocks.push(
          h(
            "div",
            {
              key: `u${i}`,
              className: "rounded bg-muted/60 px-2 py-1 font-mono text-xs",
            },
            `→ ${name}(${summarizeInput(input)})`,
          ),
        );
      } else {
        toolBlocks.push(
          h(
            "div",
            { key: `o${i}`, className: "text-xs opacity-60" },
            `block: ${btype}`,
          ),
        );
      }
    });
    return {
      primary: textBlocks.length
        ? h("div", { className: "space-y-1" }, ...textBlocks)
        : null,
      secondary: toolBlocks.length
        ? h("div", { className: "space-y-1" }, ...toolBlocks)
        : null,
    };
  }

  if (type === "user") {
    const message = e.message as Record<string, unknown> | undefined;
    const content = (message?.content ?? []) as Array<Record<string, unknown>>;
    const blocks: ReactNode[] = [];
    content.forEach((block, i) => {
      const btype = String(block.type ?? "");
      if (btype === "tool_result") {
        const isError = Boolean(block.is_error);
        const inner = block.content;
        const text =
          typeof inner === "string"
            ? inner
            : Array.isArray(inner)
              ? inner
                  .map((c) =>
                    typeof c === "object" && c && "text" in c
                      ? String((c as Record<string, unknown>).text ?? "")
                      : JSON.stringify(c),
                  )
                  .join("")
              : JSON.stringify(inner);
        const truncated = text.length > 400 ? `${text.slice(0, 400)}…` : text;
        blocks.push(
          h(
            "div",
            {
              key: `r${i}`,
              className: `rounded px-2 py-1 font-mono text-xs whitespace-pre-wrap ${
                isError ? "bg-destructive/10 text-destructive" : "bg-muted/40"
              }`,
            },
            `← ${truncated}`,
          ),
        );
      }
    });
    return {
      primary: null,
      secondary: blocks.length
        ? h("div", { className: "space-y-1" }, ...blocks)
        : null,
    };
  }

  if (type === "result") {
    const result = String((e as Record<string, unknown>).result ?? "");
    return {
      primary: null,
      secondary: h(
        "div",
        { className: "text-xs opacity-60" },
        result ? `done — ${result.slice(0, 200)}` : "done",
      ),
    };
  }

  // Unknown event — keep visible (in the secondary bucket) so we don't
  // silently drop things if the wire format gains a new shape.
  return {
    primary: null,
    secondary: h(
      "div",
      { className: "text-xs opacity-50 font-mono" },
      `${type}: ${JSON.stringify(e).slice(0, 200)}`,
    ),
  };
}

function summarizeInput(input: unknown): string {
  if (typeof input !== "object" || input === null) return String(input);
  const obj = input as Record<string, unknown>;
  // Show the first one or two interesting fields.
  for (const key of ["pattern", "path", "file_path", "command", "url"]) {
    if (key in obj) {
      const v = String(obj[key]);
      return `${key}=${v.length > 80 ? v.slice(0, 80) + "…" : v}`;
    }
  }
  const keys = Object.keys(obj).slice(0, 2);
  return keys.map((k) => `${k}=${truncate(JSON.stringify(obj[k]), 40)}`).join(", ");
}

function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n) + "…" : s;
}
