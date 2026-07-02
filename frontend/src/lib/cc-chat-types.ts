export type ChatScopeKind = "experiment" | "task" | "global";

type ChatStatus = "provisioning" | "active" | "closed" | "broken";

export interface ChatSessionSummary {
  id: string;
  title: string | null;
  status: ChatStatus;
  created_at: string;
  last_activity: string;
  turn_count: number;
}

// A rendered line in the transcript. Derived from stream-json events.
export interface ChatBubble {
  role: "user" | "assistant";
  text: string;
}
