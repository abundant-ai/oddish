"use client";

import {
  Anthropic,
  Cohere,
  DeepSeek,
  Gemini,
  Meta,
  Mistral,
  OpenAI,
  XAI,
} from "@lobehub/icons";
import { Sparkles } from "lucide-react";

type QueueKeyIconProps = {
  queueKey?: string | null;
  model?: string | null;
  agent?: string | null;
  className?: string;
  size?: number;
};

type KnownProvider =
  | "openai"
  | "anthropic"
  | "gemini"
  | "deepseek"
  | "mistral"
  | "xai"
  | "meta"
  | "cohere"
  | "unknown";

function resolveProvider({
  queueKey,
  model,
  agent,
}: Omit<QueueKeyIconProps, "className" | "size">): KnownProvider {
  const probe = `${queueKey ?? ""} ${model ?? ""} ${agent ?? ""}`.toLowerCase();

  if (
    probe.includes("openai") ||
    probe.includes(" gpt") ||
    probe.includes("/gpt") ||
    probe.includes(" o1") ||
    probe.includes(" o3") ||
    probe.startsWith("o1") ||
    probe.startsWith("o3") ||
    probe.includes("codex")
  ) {
    return "openai";
  }
  if (probe.includes("anthropic") || probe.includes("claude")) {
    return "anthropic";
  }
  if (
    probe.includes("gemini") ||
    probe.includes("google/") ||
    probe.includes("google ")
  ) {
    return "gemini";
  }
  if (probe.includes("deepseek")) {
    return "deepseek";
  }
  if (probe.includes("mistral")) {
    return "mistral";
  }
  if (probe.includes("xai") || probe.includes("grok")) {
    return "xai";
  }
  if (probe.includes("meta") || probe.includes("llama")) {
    return "meta";
  }
  if (probe.includes("cohere") || probe.includes("command-r")) {
    return "cohere";
  }
  return "unknown";
}

export function QueueKeyIcon({
  queueKey,
  model,
  agent,
  className,
  size = 14,
}: QueueKeyIconProps) {
  const resolvedProvider = resolveProvider({ queueKey, model, agent });

  if (resolvedProvider === "openai") {
    return <OpenAI size={size} className={className} />;
  }
  if (resolvedProvider === "anthropic") {
    return <Anthropic size={size} className={className} />;
  }
  if (resolvedProvider === "gemini") {
    return <Gemini size={size} className={className} />;
  }
  if (resolvedProvider === "deepseek") {
    return <DeepSeek size={size} className={className} />;
  }
  if (resolvedProvider === "mistral") {
    return <Mistral size={size} className={className} />;
  }
  if (resolvedProvider === "xai") {
    return <XAI size={size} className={className} />;
  }
  if (resolvedProvider === "meta") {
    return <Meta size={size} className={className} />;
  }
  if (resolvedProvider === "cohere") {
    return <Cohere size={size} className={className} />;
  }

  return <Sparkles size={size} className={className} />;
}
