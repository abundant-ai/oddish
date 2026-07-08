"use client";

import { useEffect, useRef, useState } from "react";
import { Loader2, Radio, Wrench } from "lucide-react";
import { HarborStageBadge } from "@/components/harbor-stage-badge";
import { fetcher } from "@/lib/api";
import { formatCostUsd } from "@/lib/format";
import { cn } from "@/lib/utils";

interface LiveEvent {
  seq: number;
  kind: string;
  payload: Record<string, unknown>;
  created_at: string;
}

interface LiveUsage {
  input_tokens: number | null;
  cache_tokens: number | null;
  cache_write_tokens: number | null;
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

const POLL_MS = 2000;

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function LiveEventRow({ event }: { event: LiveEvent }) {
  const { kind, payload } = event;

  if (kind === "tool_use") {
    return (
      <div className="flex items-start gap-1.5 text-purple-500">
        <Wrench className="mt-0.5 h-3 w-3 shrink-0" />
        <span className="min-w-0 flex-1 wrap-break-word">
          <span className="font-semibold">{str(payload.name) || "tool"}</span>
          {str(payload.input) && (
            <span className="text-muted-foreground"> {str(payload.input)}</span>
          )}
          {payload.truncated ? " …" : ""}
        </span>
      </div>
    );
  }

  if (kind === "tool_result") {
    return (
      <div
        className={cn(
          "border-border/60 border-l-2 pl-2 wrap-break-word whitespace-pre-wrap",
          payload.is_error ? "text-red-500" : "text-muted-foreground"
        )}
      >
        {str(payload.content) || "(no output)"}
        {payload.truncated ? " …" : ""}
      </div>
    );
  }

  if (kind === "summary") {
    return (
      <div className="text-muted-foreground border-border border-t pt-2 text-[11px] italic">
        {str(payload.text)}
        {payload.truncated ? " …" : ""}
      </div>
    );
  }

  return (
    <div className="wrap-break-word whitespace-pre-wrap">
      {str(payload.text)}
      {payload.truncated ? " …" : ""}
    </div>
  );
}

export function LiveTranscriptPanel({
  trialId,
  apiBaseUrl = "/api",
}: {
  trialId: string;
  apiBaseUrl?: string;
}) {
  const [events, setEvents] = useState<LiveEvent[]>([]);
  const [usage, setUsage] = useState<LiveUsage | null>(null);
  const [stage, setStage] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [connected, setConnected] = useState(false);
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
        const params = new URLSearchParams({
          after_seq: String(afterSeqRef.current),
        });
        if (attemptRef.current != null) {
          params.set("attempt", String(attemptRef.current));
        }
        const data = await fetcher<LiveResponse>(
          `${apiBaseUrl}/trials/${trialId}/live?${params.toString()}`
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
        setUsage(data.usage);
        setStage(data.harbor_stage);
        setConnected(true);
        setError(null);
        setDone(data.done);
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

  const tokens =
    usage != null ? (usage.input_tokens ?? 0) + (usage.output_tokens ?? 0) : 0;

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
                  {connected ? "Waiting for agent output…" : "Connecting…"}
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
