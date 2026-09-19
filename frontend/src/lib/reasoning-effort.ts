const STANDARD_EFFORTS = ["low", "medium", "high"];
const EXTENDED_EFFORTS = [...STANDARD_EFFORTS, "xhigh", "max"];

// These runners forward reasoning_effort; the provider still validates the
// selected model's accepted values when executing the request.
export const REASONING_EFFORT_AGENTS = [
  "claude-code",
  "codex",
  "gemini-cli",
  "antigravity-cli",
  "cursor-cli",
  "grok-build",
  "mini-swe-agent",
  "aider",
  "openhands",
  "copilot-cli",
  "dsh",
  "tbh",
  "muse-code",
];

/** Choices for the bundled runners, including their model-specific restrictions. */
export function reasoningEffortOptions(agent: string, model: string): string[] {
  agent = agent.trim().toLowerCase();
  model = model.trim().toLowerCase();
  if (!model) return [];

  if (agent === "cursor-cli") {
    // Cursor rejects a second override when effort is already in model[...].
    return /\[(?:[^\]]*,)?\s*effort\s*=/.test(model) ? [] : EXTENDED_EFFORTS;
  }
  if (agent === "grok-build") {
    return ["none", "minimal", ...EXTENDED_EFFORTS];
  }
  if (agent === "copilot-cli") return [...STANDARD_EFFORTS, "xhigh"];
  if (agent === "dsh") return ["low", "high", "max"];
  if (agent === "tbh")
    return ["none", "minimal", ...STANDARD_EFFORTS, "xhigh", "ultra"];
  // The Meta provider rejects "none"; "ultra" is client-side and runs at
  // xhigh where the provider lacks it.
  if (agent === "muse-code")
    return ["minimal", ...STANDARD_EFFORTS, "xhigh", "max", "ultra"];

  const modelRouted = ["mini-swe-agent", "aider", "openhands"].includes(agent);
  if (agent === "gemini-cli" || agent === "antigravity-cli" || modelRouted) {
    // Gemini 2.5 uses a token budget, not reasoning_effort. For Gemini 3,
    // minimal and medium are Flash-only in the bundled Gemini runners.
    if (/(?:^|\/)gemini-3(?:[.-]|$)/.test(model)) {
      return model.includes("flash")
        ? ["minimal", ...STANDARD_EFFORTS]
        : ["low", "high"];
    }
    if (!modelRouted) return [];
  }
  if (
    (agent === "claude-code" || modelRouted) &&
    /claude-(opus|sonnet)-/.test(model)
  ) {
    return /claude-opus-(5|4-7)/.test(model)
      ? EXTENDED_EFFORTS
      : STANDARD_EFFORTS;
  }
  if (agent === "codex" || modelRouted) {
    if (/(?:^|\/)gpt-5/.test(model)) return EXTENDED_EFFORTS;
    if (/(?:^|\/)o[134](?:-|$)/.test(model)) return STANDARD_EFFORTS;
  }
  return [];
}
