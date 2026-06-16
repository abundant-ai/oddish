export type ChatScopeKind = "experiment" | "task_probes";

export type ChatStatus = "provisioning" | "active" | "closed" | "broken";

export interface ChatSessionSummary {
  id: string;
  title: string | null;
  status: ChatStatus;
  created_at: string;
  last_activity: string;
  turn_count: number;
}

export interface ChatSessionDetail {
  session_id: string;
  status: ChatStatus;
  scope_kind: ChatScopeKind;
  scope_id: string;
  running: boolean;
  created_at: string;
  last_activity: string;
  closed_at: string | null;
}

// A rendered line in the transcript. Derived from stream-json events.
export interface ChatBubble {
  role: "user" | "assistant";
  text: string;
}
