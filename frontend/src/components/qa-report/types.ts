export type FeedbackVote = "agree" | "disagree";

/** Emitted whenever a reviewer votes. `target` says what was voted on. */
export type FeedbackRecord = {
  target:
    | { kind: "verdict"; classification: string }
    | { kind: "action_item"; id: string };
  vote: FeedbackVote;
  note?: string;
};

/** The hosted feedback routes' request for one vote on `trialId`'s QA output. */
export function feedbackRequestInit(
  record: FeedbackRecord,
  trialId: string
): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      body: record.note?.trim() ?? "",
      target:
        record.target.kind === "verdict" ? "qa_verdict" : "qa_action_item",
      target_key:
        record.target.kind === "verdict"
          ? record.target.classification
          : record.target.id,
      vote: record.vote,
      trial_id: trialId,
    }),
  };
}
