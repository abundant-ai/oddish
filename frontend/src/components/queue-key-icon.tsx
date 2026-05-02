"use client";

import {
  Anthropic,
  ChatGLM,
  Cohere,
  DeepSeek,
  Gemini,
  Kimi,
  Meta,
  Minimax,
  Mistral,
  OpenAI,
  Qwen,
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
  | "qwen"
  | "glm"
  | "kimi"
  | "minimax"
  | "unknown";

// Regex hits at word boundaries (start of string, after `/`, ` `, `-`, `_`, `.`)
// so we don't accidentally match substrings like "glm" inside another token.
function hasToken(probe: string, token: string): boolean {
  const escaped = token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`(^|[\\s/_.\\-])${escaped}([\\s/_.\\-]|$|\\d)`).test(probe);
}

function resolveProvider({
  queueKey,
  model,
  agent,
}: Omit<QueueKeyIconProps, "className" | "size">): KnownProvider {
  // Build the probe from queueKey + model + agent so a generic agent name
  // (e.g. "terminus-2") still resolves via its bound model. The agent name
  // alone often doesn't carry provider info, which is fine.
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
  // Alibaba's Qwen family: `qwen2.5-coder`, `Qwen/Qwen3-...`, `qwen3-coder-plus`, etc.
  if (hasToken(probe, "qwen")) {
    return "qwen";
  }
  // Zhipu's GLM family: `glm-4.5`, `glm-4.6`, `zai-org/glm-...`, `chatglm-...`,
  // and the org-prefixed forms (`zhipu/...`, `zai/...`). Bounded with hasToken
  // so we don't match unrelated substrings.
  if (
    hasToken(probe, "glm") ||
    probe.includes("chatglm") ||
    probe.includes("zhipu") ||
    probe.includes("zai-org/") ||
    probe.includes("zai/")
  ) {
    return "glm";
  }
  // Moonshot's Kimi family: `kimi-k2`, `moonshot/kimi-...`, `moonshot-v1-...`.
  if (probe.includes("kimi") || probe.includes("moonshot")) {
    return "kimi";
  }
  // MiniMax: `minimax/abab6.5`, `minimax-m1`, `minimax-text-01`, etc.
  if (probe.includes("minimax")) {
    return "minimax";
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
  if (resolvedProvider === "qwen") {
    return <Qwen size={size} className={className} />;
  }
  if (resolvedProvider === "glm") {
    return <ChatGLM size={size} className={className} />;
  }
  if (resolvedProvider === "kimi") {
    return <Kimi size={size} className={className} />;
  }
  if (resolvedProvider === "minimax") {
    return <Minimax size={size} className={className} />;
  }

  return <Sparkles size={size} className={className} />;
}
