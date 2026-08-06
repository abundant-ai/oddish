"use client";

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Info, Loader2, Radio, ShieldAlert } from "lucide-react";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Badge } from "@/components/ui/badge";
import {
  StepHeader,
  ToolCallBlock,
  ObservationBlock,
} from "@/components/trajectory-blocks";
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

interface LiveStep {
  key: number;
  kind: "turn" | "summary";
  events: LiveEvent[];
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
  if (name.includes("grok-build")) {
    return "Grok streams its replies and reasoning. Its tool calls and token usage are not in the live stream — they appear in the trajectory once the run finishes.";
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

function turnId(event: LiveEvent): string | null {
  return typeof event.payload.turn_id === "string"
    ? event.payload.turn_id
    : null;
}

function sameMessageBlock(
  event: LiveEvent,
  id: string,
  blockIndex: unknown
): boolean {
  return (
    event.kind === "message" &&
    turnId(event) === id &&
    event.payload.block_index === blockIndex
  );
}

function updateMessageBlock(previous: LiveEvent, next: LiveEvent): LiveEvent {
  if (next.payload.text_mode === "replace") {
    return { ...next, seq: previous.seq };
  }
  const previousText =
    typeof previous.payload.text === "string" ? previous.payload.text : "";
  const nextText =
    typeof next.payload.text === "string" ? next.payload.text : "";
  return {
    ...previous,
    payload: {
      ...previous.payload,
      ...next.payload,
      text: previousText + nextText,
      text_mode: "replace",
      sanitized:
        previous.payload.sanitized === true || next.payload.sanitized === true,
      truncated:
        previous.payload.truncated === true || next.payload.truncated === true,
    },
  };
}

function groupSteps(events: LiveEvent[]): LiveStep[] {
  const steps: LiveStep[] = [];
  let current: LiveStep | null = null;
  const flush = () => {
    if (current) steps.push(current);
    current = null;
  };
  for (const ev of events) {
    if (ev.kind === "summary") {
      flush();
      steps.push({ key: ev.seq, kind: "summary", events: [ev] });
      continue;
    }
    if (ev.kind === "message") {
      const id = turnId(ev);
      // Claude live-tail tags suffixes and tool blocks from one assistant
      // message with the same opaque turn id. Missing ids stay conservative.
      if (id && current?.events.some((event) => turnId(event) === id)) {
        if (
          ev.payload.text_mode === "append" ||
          ev.payload.text_mode === "replace"
        ) {
          const priorIndex = current.events.findIndex((event) =>
            sameMessageBlock(event, id, ev.payload.block_index)
          );
          if (priorIndex >= 0) {
            current.events[priorIndex] = updateMessageBlock(
              current.events[priorIndex],
              ev
            );
            continue;
          }
        }
        current.events.push(ev);
        continue;
      }
      flush();
      current = { key: ev.seq, kind: "turn", events: [ev] };
      continue;
    }
    const id = turnId(ev);
    if (
      id &&
      current &&
      !current.events.some((event) => turnId(event) === id)
    ) {
      flush();
    }
    if (!current) {
      current = { key: ev.seq, kind: "turn", events: [] };
    }
    current.events.push(ev);
  }
  flush();
  return steps;
}

function stepPreview(step: LiveStep): string | null {
  const text = step.events
    .filter((event) => event.kind === "message")
    .map((event) => safeText(event.payload.text).text)
    .join("")
    .trim();
  if (text) return text.split("\n")[0].slice(0, 80);
  const firstTool = step.events.find((e) => e.kind === "tool_use");
  if (firstTool) return safeText(firstTool.payload.name).text || "tool";
  return null;
}

function stepBadges(step: LiveStep): ReactNode {
  const tools = step.events.filter((e) => e.kind === "tool_use").length;
  if (!tools) return undefined;
  return (
    <Badge variant="secondary" className="px-1.5 py-0 text-[10px] font-normal">
      {tools} tool{tools > 1 ? "s" : ""}
    </Badge>
  );
}

function markers(
  payload: Record<string, unknown>,
  sanitized: boolean
): ReactNode {
  if (!payload.truncated && !sanitized) return undefined;
  return (
    <>
      {payload.truncated ? (
        <span className="text-muted-foreground">…</span>
      ) : null}
      {sanitized && <SanitizedBadge />}
    </>
  );
}

function LiveMessage({ events }: { events: LiveEvent[] }) {
  const parts = events.map((event) => safeText(event.payload.text));
  const text = parts.map((part) => part.text).join("");
  const truncated = events.some((event) => event.payload.truncated === true);
  const sanitized =
    events.some((event) => event.payload.sanitized === true) ||
    parts.some((part) => part.changed);
  return (
    <div className="text-sm wrap-break-word whitespace-pre-wrap">
      {text}
      {truncated ? " …" : ""}
      {sanitized && <SanitizedBadge />}
    </div>
  );
}

function LiveToolUse({ payload }: { payload: Record<string, unknown> }) {
  const name = safeText(payload.name);
  const input = safeText(payload.input);
  const sanitized = payload.sanitized === true || name.changed || input.changed;
  return (
    <ToolCallBlock
      name={name.text}
      args={prettyJson(input.text)}
      trailing={markers(payload, sanitized)}
    />
  );
}

function LiveToolResult({ payload }: { payload: Record<string, unknown> }) {
  const content = safeText(payload.content);
  const sanitized = payload.sanitized === true || content.changed;
  const truncated = payload.truncated ? (
    <span className="text-muted-foreground">truncated …</span>
  ) : null;
  return (
    <ObservationBlock
      content={content.text.trim() ? content.text : "(no output)"}
      isError={payload.is_error === true}
      trailing={
        truncated || sanitized ? (
          <>
            {truncated}
            {sanitized && <SanitizedBadge />}
          </>
        ) : undefined
      }
    />
  );
}

function LiveSummary({ payload }: { payload: Record<string, unknown> }) {
  const text = safeText(payload.text);
  const sanitized = payload.sanitized === true || text.changed;
  return (
    <div className="border-border text-muted-foreground border-t pt-2 text-[11px] italic">
      {text.text}
      {payload.truncated ? " …" : ""}
      {sanitized && <SanitizedBadge />}
    </div>
  );
}

function LiveStepContent({ step }: { step: LiveStep }) {
  const blocks: ReactNode[] = [];
  for (let i = 0; i < step.events.length; i++) {
    const event = step.events[i];
    if (event.kind === "message") {
      const messages = [event];
      while (step.events[i + 1]?.kind === "message") {
        messages.push(step.events[++i]);
      }
      blocks.push(<LiveMessage key={event.seq} events={messages} />);
    } else if (event.kind === "tool_use") {
      blocks.push(<LiveToolUse key={event.seq} payload={event.payload} />);
    } else if (event.kind === "tool_result") {
      blocks.push(<LiveToolResult key={event.seq} payload={event.payload} />);
    } else {
      blocks.push(<LiveMessage key={event.seq} events={[event]} />);
    }
  }
  return <div className="space-y-3 text-sm">{blocks}</div>;
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
  const [expanded, setExpanded] = useState<string[]>([]);

  const attemptRef = useRef<number | null>(null);
  const afterSeqRef = useRef(0);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const atBottomRef = useRef(true);
  const prevNewestRef = useRef<string | null>(null);
  const autoExpandedRef = useRef<string | null>(null);

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
          prevNewestRef.current = null;
          setExpanded([]);
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

  const steps = useMemo(() => groupSteps(events), [events]);
  const numbered = useMemo(() => {
    let n = 0;
    return steps.map((step) => ({
      step,
      num: step.kind === "turn" ? ++n : 0,
    }));
  }, [steps]);

  const newest = useMemo(() => {
    for (let i = steps.length - 1; i >= 0; i--) {
      if (steps[i].kind === "turn") return `live-${steps[i].key}`;
    }
    return null;
  }, [steps]);

  useEffect(() => {
    if (!newest || newest === prevNewestRef.current) return;
    if (!atBottomRef.current) return;
    const prev = prevNewestRef.current;
    prevNewestRef.current = newest;
    autoExpandedRef.current = newest;
    setExpanded((cur) => {
      const next = new Set(cur);
      if (prev) next.delete(prev);
      next.add(newest);
      return Array.from(next);
    });
  }, [newest]);

  useEffect(() => {
    if (atBottomRef.current && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [events]);

  const onAccordionAnimationEnd = (value: string) => {
    if (
      autoExpandedRef.current === value &&
      atBottomRef.current &&
      scrollRef.current
    ) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
      autoExpandedRef.current = null;
    }
  };

  const onExpandedChange = (values: string[]) => {
    autoExpandedRef.current = null;
    setExpanded(values);
  };

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const wasAtBottom = atBottomRef.current;
    const isAtBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    atBottomRef.current = isAtBottom;
    if (
      !wasAtBottom &&
      isAtBottom &&
      newest &&
      newest !== prevNewestRef.current
    ) {
      const prev = prevNewestRef.current;
      prevNewestRef.current = newest;
      autoExpandedRef.current = newest;
      setExpanded((cur) => {
        const next = new Set(cur);
        if (prev) next.delete(prev);
        next.add(newest);
        return Array.from(next);
      });
    }
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
      <div ref={scrollRef} onScroll={onScroll} className="flex-1 overflow-auto">
        {steps.length > 0 && (
          <Accordion
            type="multiple"
            value={expanded}
            onValueChange={onExpandedChange}
          >
            {numbered.map(({ step, num }) =>
              step.kind === "summary" ? (
                step.events[0] && (
                  <div key={step.key} className="px-4 py-2">
                    <LiveSummary payload={step.events[0].payload} />
                  </div>
                )
              ) : (
                <AccordionItem
                  key={step.key}
                  value={`live-${step.key}`}
                  className="px-4"
                >
                  <AccordionTrigger className="py-2 hover:no-underline">
                    <StepHeader
                      index={num}
                      source="agent"
                      preview={stepPreview(step)}
                      badges={stepBadges(step)}
                    />
                  </AccordionTrigger>
                  <AccordionContent
                    onAnimationEnd={() =>
                      onAccordionAnimationEnd(`live-${step.key}`)
                    }
                  >
                    <LiveStepContent step={step} />
                  </AccordionContent>
                </AccordionItem>
              )
            )}
          </Accordion>
        )}
        {events.length === 0 && !error && (
          <div className="text-muted-foreground flex items-center gap-2 px-4 py-6 text-xs">
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
          <div className="px-4 pt-2 text-xs text-red-500">
            Live stream error: {error}
          </div>
        )}
      </div>
    </div>
  );
}
