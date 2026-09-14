/** Effort choices for the agent/model pairs exposed by the run controls. */
export function reasoningEffortOptions(agent: string, model: string): string[] {
  if (agent === "claude-code" && /claude-(opus|sonnet)-/.test(model)) {
    return /claude-opus-(5|4-7)/.test(model)
      ? ["low", "medium", "high", "xhigh", "max"]
      : ["low", "medium", "high"];
  }
  if (agent === "codex" && /gpt-5/.test(model)) {
    return ["low", "medium", "high", "xhigh"];
  }
  return [];
}
