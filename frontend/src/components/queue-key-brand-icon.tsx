"use client";

import {
  Anthropic,
  Baichuan,
  Cohere,
  Cursor,
  DeepSeek,
  Inflection,
  Liquid,
  Meta,
  Mistral,
  NousResearch,
  OpenAI,
  OpenRouter,
  Qwen,
  XAI,
  Yi,
} from "@lobehub/icons";

const BRAND_ICONS = {
  anthropic: Anthropic,
  baichuan: Baichuan,
  cohere: Cohere,
  cursor: Cursor,
  deepseek: DeepSeek,
  inflection: Inflection,
  liquid: Liquid,
  meta: Meta,
  mistral: Mistral,
  nous: NousResearch,
  openai: OpenAI,
  openrouter: OpenRouter,
  qwen: Qwen,
  xai: XAI,
  yi: Yi,
} as const;

export type BrandProvider = keyof typeof BRAND_ICONS;

export default function QueueKeyBrandIcon({
  provider,
  size,
  className,
}: {
  provider: BrandProvider;
  size?: number;
  className?: string;
}) {
  const Icon = BRAND_ICONS[provider];
  return <Icon size={size} className={className} />;
}
