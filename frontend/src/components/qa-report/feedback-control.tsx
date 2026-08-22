"use client";

import { useId, useState } from "react";
import { ThumbsDown, ThumbsUp } from "lucide-react";
import { cn } from "@/lib/utils";
import type { FeedbackVote } from "./types";

type SubmissionState =
  | { status: "idle" }
  | { status: "submitting" }
  | { status: "submitted" }
  | { status: "error"; message: string };

/**
 * Agree / disagree control for one reviewable claim. Disagreeing opens an
 * optional free-text note, because a bare downvote is not actionable.
 */
export function FeedbackControl({
  label,
  onSubmit,
  className,
}: {
  /** describes what is being voted on, for screen readers */
  label: string;
  onSubmit: (vote: FeedbackVote, note?: string) => Promise<void>;
  className?: string;
}) {
  const [vote, setVote] = useState<FeedbackVote | null>(null);
  const [note, setNote] = useState("");
  const [submission, setSubmission] = useState<SubmissionState>({
    status: "idle",
  });
  const noteId = useId();
  const isLocked =
    submission.status === "submitting" || submission.status === "submitted";

  const base =
    "focus-visible:ring-ring inline-flex items-center gap-1.5 rounded-md border px-2 py-1 font-mono text-[10px] tracking-wide transition-colors focus-visible:ring-2 focus-visible:outline-none";
  const idle =
    "border-border text-muted-foreground hover:bg-secondary hover:text-foreground";

  async function submit(next: FeedbackVote, submittedNote?: string) {
    setSubmission({ status: "submitting" });
    try {
      await onSubmit(next, submittedNote);
      setSubmission({ status: "submitted" });
    } catch (error) {
      setSubmission({
        status: "error",
        message:
          error instanceof Error ? error.message : "Failed to record feedback",
      });
    }
  }

  function pick(next: FeedbackVote) {
    if (isLocked) return;
    setSubmission({ status: "idle" });
    setVote(next);
    if (next === "agree") {
      setNote("");
      void submit("agree");
    }
  }

  return (
    <div className={cn("flex flex-col gap-2", className)}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-muted-foreground mr-1 font-mono text-[10px] tracking-wider">
          {submission.status === "submitting"
            ? "RECORDING…"
            : submission.status === "submitted"
              ? "FEEDBACK RECORDED"
              : vote === null
                ? "IS THIS RIGHT?"
                : vote === "agree"
                  ? "AGREE SELECTED"
                  : "DISAGREE SELECTED"}
        </span>
        <button
          type="button"
          aria-pressed={vote === "agree"}
          disabled={isLocked}
          onClick={() => pick("agree")}
          className={cn(
            base,
            vote === "agree"
              ? "border-emerald-500/60 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
              : idle,
            "disabled:cursor-default disabled:opacity-70"
          )}
        >
          <ThumbsUp aria-hidden="true" className="size-3" />
          Agree
          <span className="sr-only">with {label}</span>
        </button>
        <button
          type="button"
          aria-pressed={vote === "disagree"}
          disabled={isLocked}
          onClick={() => pick("disagree")}
          className={cn(
            base,
            vote === "disagree"
              ? "border-destructive/60 bg-destructive/15 text-destructive"
              : idle,
            "disabled:cursor-default disabled:opacity-70"
          )}
        >
          <ThumbsDown aria-hidden="true" className="size-3" />
          Disagree
          <span className="sr-only">with {label}</span>
        </button>
      </div>

      {vote === "disagree" && submission.status !== "submitted" ? (
        <div className="flex flex-col gap-2">
          <label
            htmlFor={noteId}
            className="text-muted-foreground font-mono text-[10px] tracking-wider"
          >
            WHY? (NOT REQUIRED)
          </label>
          <textarea
            id={noteId}
            value={note}
            onChange={(e) => setNote(e.target.value)}
            disabled={submission.status === "submitting"}
            rows={2}
            placeholder="e.g. the agent had no way to know the budget remaining"
            className="border-input bg-background text-foreground placeholder:text-muted-foreground focus-visible:ring-ring w-full resize-y rounded-md border px-2.5 py-1.5 text-xs leading-relaxed focus-visible:ring-2 focus-visible:outline-none"
          />
          <div>
            <button
              type="button"
              onClick={() => {
                const trimmedNote = note.trim();
                setNote(trimmedNote);
                void submit("disagree", trimmedNote || undefined);
              }}
              disabled={submission.status === "submitting"}
              className="bg-primary text-primary-foreground focus-visible:ring-ring rounded-md px-2.5 py-1 font-mono text-[10px] tracking-wide transition-opacity hover:opacity-90 focus-visible:ring-2 focus-visible:outline-none"
            >
              Submit
            </button>
          </div>
        </div>
      ) : null}

      {submission.status === "submitted" ? (
        <p className="text-muted-foreground text-xs leading-relaxed">
          {note ? (
            <>
              <span className="font-mono text-[10px] tracking-wider">
                YOUR NOTE{" "}
              </span>
              {note}
            </>
          ) : (
            "Feedback submitted."
          )}
        </p>
      ) : null}
      {submission.status === "error" ? (
        <p className="text-destructive text-xs" role="alert">
          {submission.message}. Try again.
        </p>
      ) : null}
    </div>
  );
}
