"use client";

import {
  Anthropic,
  Baichuan,
  ChatGLM,
  Cohere,
  DeepSeek,
  Gemini,
  Inflection,
  Kimi,
  Liquid,
  Meta,
  Minimax,
  Mistral,
  NousResearch,
  OpenAI,
  OpenRouter,
  Qwen,
  XAI,
  Yi,
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
  | "yi"
  | "baichuan"
  | "nous"
  | "inflection"
  | "liquid"
  | "openrouter"
  | "unknown";

// Regex hits at word boundaries (start of string, after `/`, ` `, `-`, `_`, `.`)
// so we don't accidentally match substrings like "glm" inside another token.
function hasToken(probe: string, token: string): boolean {
  const escaped = token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`(^|[\\s/_.\\-])${escaped}([\\s/_.\\-]|$|\\d)`).test(probe);
}

/**
 * Resolve a provider from a single source string. Vendor-specific tokens are
 * checked BEFORE the generic OpenAI catch-all because the backend stores
 * `provider="openai"` for every openrouter-routed trial (cost attribution
 * is bucketed by API surface, not by underlying vendor). If we let "openai"
 * win on substring match, every openrouter-vended model would render as the
 * OpenAI logo regardless of the real upstream vendor.
 */
function matchProviderFromSource(raw: string): KnownProvider {
  const lower = raw.toLowerCase();

  // Strip the openrouter prefix so "openrouter/deepseek/..." resolves on
  // its underlying vendor rather than triggering an "openrouter" fallback
  // that would hide the real provider.
  const probe = lower
    .replace(/^openrouter\//, "")
    .replace(/[\s/_.-]openrouter\//g, "/");

  if (probe.includes("deepseek")) {
    return "deepseek";
  }
  // Alibaba's Qwen family: `qwen2.5-coder`, `Qwen/Qwen3-...`, `qwen3-coder-plus`.
  if (hasToken(probe, "qwen")) {
    return "qwen";
  }
  // Zhipu's GLM family: `glm-4.5`, `chatglm-...`, plus the org-prefixed
  // forms (`zai-org/`, `zai/`, `z-ai/`, `zhipu/`).
  if (
    hasToken(probe, "glm") ||
    probe.includes("chatglm") ||
    probe.includes("zhipu") ||
    probe.includes("zai-org/") ||
    probe.includes("zai/") ||
    probe.includes("z-ai/")
  ) {
    return "glm";
  }
  // Moonshot's Kimi family: `kimi-k2`, `moonshot/kimi-...`, `moonshotai/...`.
  if (probe.includes("kimi") || probe.includes("moonshot")) {
    return "kimi";
  }
  if (probe.includes("minimax")) {
    return "minimax";
  }
  if (probe.includes("mistral")) {
    return "mistral";
  }
  if (
    probe.includes("xai") ||
    probe.includes("grok") ||
    probe.includes("x-ai/")
  ) {
    return "xai";
  }
  if (
    probe.includes("meta") ||
    probe.includes("llama") ||
    probe.includes("meta-llama")
  ) {
    return "meta";
  }
  if (probe.includes("cohere") || probe.includes("command-r")) {
    return "cohere";
  }
  if (probe.includes("anthropic") || probe.includes("claude")) {
    return "anthropic";
  }
  // Gemini / Google — `google/`, `gemini-...`, `gemma-...`.
  if (
    probe.includes("gemini") ||
    probe.includes("google/") ||
    probe.includes("google ") ||
    probe.startsWith("google/") ||
    hasToken(probe, "gemma")
  ) {
    return "gemini";
  }
  // 01.AI's Yi family: `yi-large`, `01-ai/yi-...`.
  if (hasToken(probe, "yi") || probe.includes("01-ai/")) {
    return "yi";
  }
  if (probe.includes("baichuan")) {
    return "baichuan";
  }
  if (probe.includes("nous") || probe.includes("hermes")) {
    return "nous";
  }
  if (probe.includes("inflection") || probe.includes("pi-")) {
    return "inflection";
  }
  if (probe.includes("liquid") || hasToken(probe, "lfm")) {
    return "liquid";
  }
  // OpenAI is intentionally checked LAST among well-known vendors.
  // The backend tags every openrouter trial with provider="openai" for
  // cost-attribution purposes, so we let vendor-specific names win first
  // and only fall back to OpenAI when no other token matches.
  if (
    probe.includes("openai") ||
    hasToken(probe, "gpt") ||
    probe.startsWith("o1") ||
    probe.startsWith("o3") ||
    probe.startsWith("o4") ||
    probe.includes("codex") ||
    probe.includes("chatgpt") ||
    probe.includes("dall-e")
  ) {
    return "openai";
  }
  // If none of the specific vendors matched but the source clearly came
  // from openrouter, render the OpenRouter glyph as a useful fallback.
  if (lower.includes("openrouter")) {
    return "openrouter";
  }
  return "unknown";
}

/**
 * Resolve a provider by inspecting `model` first (most authoritative — the
 * full vendored model string carries the real upstream identity), then
 * `queueKey`, then `agent`. Each source is matched independently so that
 * an unhelpful queueKey (e.g. backend-stored "openai" for openrouter
 * traffic) doesn't poison a perfectly resolvable model string.
 */
function resolveProvider({
  queueKey,
  model,
  agent,
}: Omit<QueueKeyIconProps, "className" | "size">): KnownProvider {
  const sources = [model, queueKey, agent]
    .map((source) => source?.trim() ?? "")
    .filter((source) => source.length > 0);

  for (const source of sources) {
    const resolved = matchProviderFromSource(source);
    if (resolved !== "unknown") {
      return resolved;
    }
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
  if (resolvedProvider === "yi") {
    return <Yi size={size} className={className} />;
  }
  if (resolvedProvider === "baichuan") {
    return <Baichuan size={size} className={className} />;
  }
  if (resolvedProvider === "nous") {
    return <NousResearch size={size} className={className} />;
  }
  if (resolvedProvider === "inflection") {
    return <Inflection size={size} className={className} />;
  }
  if (resolvedProvider === "liquid") {
    return <Liquid size={size} className={className} />;
  }
  if (resolvedProvider === "openrouter") {
    return <OpenRouter size={size} className={className} />;
  }

  return <Sparkles size={size} className={className} />;
}
