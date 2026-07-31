export type FeedbackVote = "agree" | "disagree";

/** Emitted whenever a reviewer votes. `target` says what was voted on. */
export type FeedbackRecord = {
  target:
    | { kind: "verdict"; classification: string }
    | { kind: "action_item"; id: string };
  vote: FeedbackVote;
  note?: string;
};
