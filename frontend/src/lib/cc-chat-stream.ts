export type ChatStreamEvent =
  | { kind: "message"; data: unknown }
  | { kind: "error"; data: unknown }
  | { kind: "done" };

export async function streamCCChatMessage(
  url: string,
  body: { content: string },
  onEvent: (e: ChatStreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok) {
    throw new Error(`cc-chat send failed: ${res.status}`);
  }
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE frames are separated by a blank line.
    let blankIdx: number;
    while ((blankIdx = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, blankIdx);
      buffer = buffer.slice(blankIdx + 2);

      let event = "message";
      let dataLines: string[] = [];
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) {
          event = line.slice("event:".length).trim();
        } else if (line.startsWith("data:")) {
          dataLines.push(line.slice("data:".length).trim());
        }
      }
      const dataRaw = dataLines.join("\n");
      let parsed: unknown = dataRaw;
      try {
        parsed = JSON.parse(dataRaw);
      } catch {
        // leave as raw string
      }

      if (event === "done") {
        onEvent({ kind: "done" });
        return;
      } else if (event === "error") {
        onEvent({ kind: "error", data: parsed });
      } else {
        onEvent({ kind: "message", data: parsed });
      }
    }
  }
  onEvent({ kind: "done" });
}
