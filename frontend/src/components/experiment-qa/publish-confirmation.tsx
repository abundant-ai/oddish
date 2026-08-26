import Link from "next/link";
import { Link2 } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import type { ExperimentQaReport } from "@/lib/types";

type QaFormAction = (formData: FormData) => void | Promise<void>;

export function ExperimentQaPublishConfirmation({
  mode,
  report,
  cancelHref,
  confirmAction,
  canPublish,
  error,
}: {
  mode: "publish" | "unpublish";
  report: ExperimentQaReport;
  cancelHref: string;
  confirmAction: QaFormAction;
  canPublish: boolean;
  error?: string | null;
}) {
  const visibleTasks = report.tasks
    .filter((task) => task.is_visible)
    .map((task) => ({
      ...task,
      items: task.items.filter((item) => item.is_visible),
    }))
    .filter((task) => task.items.length > 0);
  const visibleItems = visibleTasks.flatMap((task) => task.items);
  const evidenceCount = visibleItems.filter(
    (item) => item.include_evidence && item.evidence
  ).length;

  return (
    <section className="border-paper-line bg-paper-surface mx-auto max-w-lg rounded-[10px] border p-5 shadow-sm">
      <h1 className="text-paper-ink font-mono text-[18px] font-semibold">
        {mode === "publish"
          ? report.is_public
            ? "Publish changes?"
            : "Publish QA?"
          : "Unpublish QA?"}
      </h1>
      <p className="text-paper-ink-2 mt-2 text-[13px] leading-relaxed">
        {mode === "publish"
          ? "This creates a new immutable public snapshot. Later draft changes stay private until you publish again."
          : "This disables the current public QA link. The next publish gets a new QA token."}
      </p>

      {error ? (
        <Alert variant="destructive" className="mt-4">
          <AlertTitle>QA could not be updated</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}

      {mode === "publish" ? (
        <div className="border-paper-line bg-paper-surface-2 mt-4 grid gap-2 rounded-lg border p-3 text-[12px]">
          <div className="flex justify-between gap-4">
            <span className="text-paper-ink-2">Tasks included</span>
            <span className="text-paper-ink font-mono font-semibold">
              {visibleTasks.length}
            </span>
          </div>
          <div className="flex justify-between gap-4">
            <span className="text-paper-ink-2">QA checks included</span>
            <span className="text-paper-ink font-mono font-semibold">
              {visibleItems.length}
            </span>
          </div>
          <div className="flex justify-between gap-4">
            <span className="text-paper-ink-2">Evidence blocks included</span>
            <span className="text-paper-ink font-mono font-semibold">
              {evidenceCount}
            </span>
          </div>
          <div className="border-paper-line-2 flex justify-between gap-4 border-t pt-2">
            <span className="text-paper-ink-2">
              Internal notes, ids, tokens, source refs, hidden rows
            </span>
            <span className="text-paper-pass font-mono font-semibold">
              Never included
            </span>
          </div>
        </div>
      ) : null}

      {mode === "publish" && !canPublish ? (
        <p className="text-paper-fail mt-4 text-[12px]">
          Publish the experiment first. QA cannot be published without a
          copyable public URL.
        </p>
      ) : null}

      {mode === "publish" && report.scope_stale ? (
        <p className="text-paper-fail mt-4 text-[12px]">
          The experiment task list changed. Cancel, sync QA, and review the
          draft before publishing.
        </p>
      ) : null}

      {mode === "publish" && visibleItems.length === 0 ? (
        <p className="text-paper-fail mt-4 text-[12px]">
          Select at least one QA check before publishing.
        </p>
      ) : null}

      <div className="mt-5 flex justify-end gap-2">
        <Button variant="outline" asChild>
          <Link href={cancelHref}>Cancel</Link>
        </Button>
        <form action={confirmAction}>
          <input
            type="hidden"
            name="expected_draft_version"
            value={report.draft_version}
          />
          <input
            type="hidden"
            name="expected_public_token"
            value={report.public_token ?? ""}
          />
          <Button
            type="submit"
            variant={mode === "unpublish" ? "destructive" : "default"}
            disabled={
              mode === "publish" &&
              (!canPublish || report.scope_stale || visibleItems.length === 0)
            }
          >
            {mode === "publish" ? (
              <Link2 className="size-4" aria-hidden="true" />
            ) : null}
            {mode === "publish"
              ? report.is_public
                ? "Publish changes"
                : "Publish QA"
              : "Unpublish"}
          </Button>
        </form>
      </div>
    </section>
  );
}
