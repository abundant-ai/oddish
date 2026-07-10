"use client";

import { useEffect, useRef, useState } from "react";
import {
  ChevronRight,
  Info,
  Loader2,
  Radio,
  ShieldAlert,
  Wrench,
} from "lucide-react";
import { CodeBlock } from "@/components/code-block";
import { HarborStageBadge } from "@/components/harbor-stage-badge";
import { fetcher } from "@/lib/api";
import { formatCostUsd } from "@/lib/format";
import { cn } from "@/lib/utils";

interface LiveEvent {
  seq: number;
  kind: string;
  payload: Record<string, unknown>;
}

interface LiveUsage {
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
}

interface LiveResponse {
  attempt: number;
  events: LiveEvent[];
  next_seq: number;
  usage: LiveUsage;
  harbor_stage: string | null;
  done: boolean;
}

const POLL_MS = 10_000;
const SANITIZED_TITLE = "Unsafe control characters were rendered safely.";

function agentNotice(agent: string | null | undefined): string | null {
  const name = (agent ?? "").toLowerCase();
  if (name.includes("mini-swe-agent")) {
    return "Mini-swe sends updates after each step finishes. The first update can take a few minutes.";
  }
  if (name.includes("cursor-cli")) {
    return "Cursor sends messages and tool calls after they finish. Work in progress may not appear right away.";
  }
  return null;
}

function safeText(value: unknown): { text: string; changed: boolean } {
  if (typeof value !== "string") return { text: "", changed: false };
  let changed = false;
  const text = Array.from(value, (ch) => {
    const code = ch.codePointAt(0) ?? 0;
    if (ch === "\n" || ch === "\r" || ch === "\t") return ch;
    if (code >= 0xd800 && code <= 0xdfff) {
      changed = true;
      return "\ufffd";
    }
    if (code === 0x7f) {
      changed = true;
      return "\u2421";
    }
    if (code < 0x20) {
      changed = true;
      return String.fromCharCode(0x2400 + code);
    }
    if (code >= 0x80 && code <= 0x9f) {
      changed = true;
      return `\\u${code.toString(16).toUpperCase().padStart(4, "0")}`;
    }
    return ch;
  }).join("");
  return { text, changed };
}

function SanitizedBadge() {
  return (
    <span
      title={SANITIZED_TITLE}
      aria-label={SANITIZED_TITLE}
      className="ml-1 inline-flex items-center gap-1 align-baseline text-[10px] font-medium text-amber-600"
    >
      <ShieldAlert className="h-3 w-3" />
      sanitized
    </span>
  );
}

function prettyJson(text: string): string {
  const trimmed = text.trim();
  if (trimmed[0] !== "{" && trimmed[0] !== "[") return text;
  try {
    return JSON.stringify(JSON.parse(trimmed), null, 2);
  } catch {
    return text;
  }
}

function LiveToolUse({
  payload,
  name,
  input,
  sanitized,
}: {
  payload: Record<string, unknown>;
  name: string;
  input: string;
  sanitized: boolean;
}) {
  const [open, setOpen] = useState(false);
  const args = prettyJson(input);
  const hasArgs = args.trim().length > 0;
  const collapsible = hasArgs && args.length > 120;
  const showArgs = hasArgs && (!collapsible || open);

  const header = (
    <>
      {collapsible ? (
        <ChevronRight
          className={cn(
            "h-3 w-3 shrink-0 text-purple-500 transition-transform",
            open && "rotate-90"
          )}
        />
      ) : (
        <Wrench className="h-3 w-3 shrink-0 text-purple-500" />
      )}
      <span className="font-mono font-semibold text-purple-500">
        {name || "tool"}
      </span>
      {collapsible && !open && (
        <span className="text-muted-foreground text-[10px]">
          (click to expand)
        </span>
      )}
      {payload.truncated ? (
        <span className="text-muted-foreground">…</span>
      ) : null}
      {sanitized && <SanitizedBadge />}
    </>
  );
  const headerClass = cn(
    "flex w-full items-center gap-1.5 px-2 py-1 text-left bg-purple-500/10",
    collapsible && "hover:bg-purple-500/15"
  );

  return (
    <div className="overflow-hidden rounded border border-purple-500/20">
      {collapsible ? (
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className={headerClass}
        >
          {header}
        </button>
      ) : (
        <div className={headerClass}>{header}</div>
      )}
      {showArgs && (
        <CodeBlock
          code={args}
          language="json"
          className="rounded-none"
          maxHeight="14rem"
        />
      )}
    </div>
  );
}

function LiveToolResult({
  payload,
  content,
  sanitized,
}: {
  payload: Record<string, unknown>;
  content: string;
  sanitized: boolean;
}) {
  const errored = payload.is_error === true;
  return (
    <div
      className={cn(
        "space-y-1",
        errored && "border-l-2 border-red-500/60 pl-2"
      )}
    >
      <CodeBlock
        code={content || "(no output)"}
        language="bash"
        maxHeight="14rem"
      />
      {(errored || payload.truncated || sanitized) && (
        <div className="flex items-center gap-1.5 pl-0.5 text-[10px]">
          {errored && <span className="font-medium text-red-500">error</span>}
          {payload.truncated ? (
            <span className="text-muted-foreground">truncated …</span>
          ) : null}
          {sanitized && <SanitizedBadge />}
        </div>
      )}
    </div>
  );
}

function LiveEventRow({ event }: { event: LiveEvent }) {
  const { kind, payload } = event;

  if (kind === "tool_use") {
    const name = safeText(payload.name);
    const input = safeText(payload.input);
    return (
      <LiveToolUse
        payload={payload}
        name={name.text}
        input={input.text}
        sanitized={payload.sanitized === true || name.changed || input.changed}
      />
    );
  }

  if (kind === "tool_result") {
    const content = safeText(payload.content);
    return (
      <LiveToolResult
        payload={payload}
        content={content.text.trim()}
        sanitized={payload.sanitized === true || content.changed}
      />
    );
  }

  const text = safeText(payload.text);
  const sanitized = payload.sanitized === true || text.changed;
  return (
    <div
      className={
        kind === "summary"
          ? "text-muted-foreground border-border border-t pt-2 text-[11px] italic"
          : "wrap-break-word whitespace-pre-wrap"
      }
    >
      {text.text}
      {payload.truncated ? " …" : ""}
      {sanitized && <SanitizedBadge />}
    </div>
  );
}

export function LiveTranscriptPanel({
  trialId,
  agent,
  apiBaseUrl = "/api",
}: {
  trialId: string;
  agent?: string | null;
  apiBaseUrl?: string;
}) {
  const [events, setEvents] = useState<LiveEvent[]>([]);
  const [last, setLast] = useState<LiveResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const attemptRef = useRef<number | null>(null);
  const afterSeqRef = useRef(0);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const atBottomRef = useRef(true);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const poll = async () => {
      try {
        const params =
          `after_seq=${afterSeqRef.current}` +
          (attemptRef.current != null ? `&attempt=${attemptRef.current}` : "");
        const data = await fetcher<LiveResponse>(
          `${apiBaseUrl}/trials/${trialId}/live?${params}`
        );
        if (cancelled) return;

        const restart =
          attemptRef.current != null && data.attempt > attemptRef.current;
        attemptRef.current = data.attempt;
        afterSeqRef.current = data.next_seq;
        if (restart) {
          setEvents(data.events);
        } else if (data.events.length) {
          setEvents((prev) => [...prev, ...data.events]);
        }
        setLast(data);
        setError(null);
        timer = setTimeout(poll, data.events.length ? 0 : POLL_MS);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "Failed to load");
        timer = setTimeout(poll, POLL_MS);
      }
    };

    poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [trialId, apiBaseUrl]);

  useEffect(() => {
    if (atBottomRef.current && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [events]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    atBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  };

  const usage = last?.usage;
  const done = last?.done ?? false;
  const stage = last?.harbor_stage;
  const tokens = (usage?.input_tokens ?? 0) + (usage?.output_tokens ?? 0);
  const notice = agentNotice(agent);

  return (
    <div className="flex h-full flex-col">
      <div className="border-border flex flex-wrap items-center gap-2 border-b px-4 py-2 text-xs">
        <span className="flex items-center gap-1.5 font-medium">
          <Radio
            className={cn(
              "h-3.5 w-3.5",
              done ? "text-muted-foreground" : "animate-pulse text-red-500"
            )}
          />
          {done ? "Ended" : "Live"}
        </span>
        {stage && <HarborStageBadge stage={stage} />}
        {usage?.cost_usd != null && (
          <span className="font-mono font-semibold tabular-nums">
            ~{formatCostUsd(usage.cost_usd)}
          </span>
        )}
        {tokens > 0 && (
          <span className="text-muted-foreground font-mono tabular-nums">
            {tokens.toLocaleString()} tok
          </span>
        )}
      </div>
      {notice && (
        <div
          role="note"
          className="border-border bg-muted/30 text-muted-foreground flex items-start gap-2 border-b px-4 py-2 text-xs"
        >
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          <span>{notice}</span>
        </div>
      )}
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="flex-1 space-y-2 overflow-auto px-4 py-3 font-mono text-xs"
      >
        {events.map((event) => (
          <LiveEventRow key={event.seq} event={event} />
        ))}
        {events.length === 0 && !error && (
          <div className="text-muted-foreground flex items-center gap-2 py-6">
            {done ? (
              <span>No live transcript for this trial.</span>
            ) : (
              <>
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                <span>
                  {last != null ? "Waiting for agent output…" : "Connecting…"}
                </span>
              </>
            )}
          </div>
        )}
        {error && (
          <div className="pt-2 text-red-500">Live stream error: {error}</div>
        )}
      </div>
    </div>
  );
}
