import Link from "next/link";
import {
  AlertTriangle,
  ExternalLink,
  Eye,
  EyeOff,
  Pencil,
  RefreshCw,
  Save,
  Sparkles,
} from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ExperimentQaGlancePanel } from "./glance-panel";
import { ExperimentQaCopyLinkButton } from "./copy-link-button";
import { ExperimentQaResultChip, ExperimentQaStatusChip } from "./result-chip";
import {
  EXPERIMENT_QA_SOURCE_LABEL,
  buildPublicQaHref,
  experimentQaFormField,
  experimentQaSignal,
} from "@/lib/experiment-qa";
import type { ExperimentQaReport } from "@/lib/types";

type QaFormAction = (formData: FormData) => void | Promise<void>;

function formatEditedAt(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("en", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "UTC",
  }).format(date);
}

function VisibilityCheckbox({
  name,
  checked,
  label,
}: {
  name: string;
  checked: boolean;
  label: string;
}) {
  return (
    <label className="text-paper-ink-2 inline-flex cursor-pointer items-center gap-1.5 text-[11px]">
      <input
        type="checkbox"
        name={name}
        value="true"
        defaultChecked={checked}
        className="peer size-4 accent-[color:var(--paper-pass)]"
      />
      <span className="sr-only">{label}</span>
      <EyeOff className="size-3.5 peer-checked:hidden" aria-hidden="true" />
      <Eye className="hidden size-3.5 peer-checked:block" aria-hidden="true" />
    </label>
  );
}

export function ExperimentQaEmptyState({
  experimentName,
  canEdit,
  createAction,
}: {
  experimentName: string;
  canEdit: boolean;
  createAction: QaFormAction;
}) {
  return (
    <div className="space-y-4">
      <header>
        <p className="text-paper-ink-3 font-mono text-[11px]">
          Experiments / {experimentName}
        </p>
        <h1 className="text-paper-ink font-mono text-[26px] font-semibold tracking-[-0.02em]">
          QA
        </h1>
      </header>
      <section className="border-paper-line bg-paper-surface flex flex-col items-center gap-3 rounded-[10px] border px-6 py-12 text-center">
        <span className="border-paper-line bg-paper-surface-2 text-paper-ink-3 flex size-10 items-center justify-center rounded-lg border">
          <Sparkles className="size-[18px]" aria-hidden="true" />
        </span>
        <div className="max-w-lg">
          <h2 className="text-paper-ink text-[15px] font-semibold">
            No QA yet for this experiment
          </h2>
          <p className="text-paper-ink-2 mt-1 text-[13px] leading-relaxed">
            Create a private draft from the completed task reviews, task
            verdicts, and trial reviews already linked to this experiment.
            Nothing is published automatically.
          </p>
        </div>
        {canEdit ? (
          <form action={createAction}>
            <Button type="submit" size="lg">
              Create QA
            </Button>
          </form>
        ) : (
          <p className="text-paper-ink-3 text-[12px]">
            An organization admin can create this QA.
          </p>
        )}
      </section>
    </div>
  );
}

export function ExperimentQaPrivateEditor({
  report,
  experimentName,
  experimentPublicToken,
  canEdit,
  previewHref,
  saveAction,
  previewAction,
  publishAction,
  unpublishAction,
  syncAction,
  notice,
}: {
  report: ExperimentQaReport;
  experimentName: string;
  experimentPublicToken: string | null;
  canEdit: boolean;
  previewHref: string;
  saveAction: QaFormAction;
  previewAction: QaFormAction;
  publishAction: QaFormAction;
  unpublishAction: QaFormAction;
  syncAction: QaFormAction;
  notice?: { tone: "success" | "error"; title: string; detail: string } | null;
}) {
  const sortedTasks = [...report.tasks].sort(
    (a, b) => a.sort_order - b.sort_order
  );
  const includedTasks = sortedTasks
    .filter((task) => task.is_visible)
    .map((task) => ({
      ...task,
      items: task.items.filter((item) => item.is_visible),
    }))
    .filter((task) => task.items.length > 0);
  const includedItemCount = includedTasks.reduce(
    (count, task) => count + task.items.length,
    0
  );
  const status = report.is_public
    ? report.has_unpublished_changes
      ? "changed"
      : "published"
    : "draft";
  const publicHref =
    experimentPublicToken && report.public_token
      ? buildPublicQaHref(experimentPublicToken, report.public_token)
      : null;

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-end gap-3">
        <div className="min-w-0">
          <p className="text-paper-ink-3 truncate font-mono text-[11px]">
            Experiments / {experimentName}
          </p>
          <div className="flex flex-wrap items-center gap-2.5">
            <h1 className="text-paper-ink font-mono text-[26px] font-semibold tracking-[-0.02em]">
              QA
            </h1>
            <ExperimentQaStatusChip status={status} />
          </div>
          <p className="text-paper-ink-3 font-mono text-[11px]">
            Last edited {formatEditedAt(report.updated_at)} UTC
            {report.published_at
              ? ` · Last published ${formatEditedAt(report.published_at)} UTC`
              : " · Never published"}
          </p>
        </div>
        <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
          {publicHref ? (
            <>
              <Button variant="outline" size="sm" asChild className="h-8">
                <Link href={publicHref} target="_blank">
                  <ExternalLink className="size-3.5" aria-hidden="true" />
                  View public QA
                </Link>
              </Button>
              <ExperimentQaCopyLinkButton href={publicHref} />
            </>
          ) : null}
          {canEdit && report.is_public ? (
            <Button
              type="submit"
              form="experiment-qa-draft"
              formAction={unpublishAction}
              variant="outline"
              size="sm"
              className="border-paper-fail/40 text-paper-fail hover:bg-paper-fail/5 h-8"
            >
              Unpublish
            </Button>
          ) : null}
          {canEdit ? (
            <>
              <Button
                type="submit"
                form="experiment-qa-draft"
                formAction={previewAction}
                variant="outline"
                size="sm"
                className="h-8"
              >
                <Eye className="size-3.5" aria-hidden="true" />
                Preview
              </Button>
              <Button
                type="submit"
                form="experiment-qa-draft"
                variant="outline"
                size="sm"
                className="h-8"
              >
                <Save className="size-3.5" aria-hidden="true" />
                Save draft
              </Button>
              <Button
                type="submit"
                form="experiment-qa-draft"
                formAction={publishAction}
                size="sm"
                className="h-8"
                disabled={
                  !experimentPublicToken ||
                  report.scope_stale ||
                  includedItemCount === 0
                }
                title={
                  report.scope_stale
                    ? "Sync QA before publishing this changed experiment scope"
                    : includedItemCount === 0
                      ? "Select at least one QA check before publishing"
                      : experimentPublicToken
                        ? undefined
                        : "Publish the experiment before publishing its QA report"
                }
              >
                {report.is_public && report.has_unpublished_changes
                  ? "Publish changes"
                  : "Publish"}
              </Button>
            </>
          ) : (
            <Button variant="outline" size="sm" asChild className="h-8">
              <Link href={previewHref}>
                <Eye className="size-3.5" aria-hidden="true" />
                Preview
              </Link>
            </Button>
          )}
        </div>
      </header>

      {canEdit && !experimentPublicToken ? (
        <p className="text-paper-ink-3 text-right text-[11px]">
          Publish the experiment first to create a public QA link.
        </p>
      ) : null}

      {notice ? (
        <Alert variant={notice.tone === "error" ? "destructive" : "default"}>
          <AlertTitle>{notice.title}</AlertTitle>
          <AlertDescription>{notice.detail}</AlertDescription>
        </Alert>
      ) : null}

      {!canEdit ? (
        <Alert>
          <AlertTitle>Read-only QA</AlertTitle>
          <AlertDescription>
            An organization admin can edit, sync, publish, or unpublish this QA.
          </AlertDescription>
        </Alert>
      ) : null}

      {report.is_public && report.has_unpublished_changes ? (
        <div className="text-paper-ink-2 flex flex-wrap items-center gap-2.5 rounded-lg border border-[color:color-mix(in_oklch,var(--paper-partial)_45%,var(--paper-line))] bg-[color:color-mix(in_oklch,var(--paper-partial)_6%,var(--paper-surface))] px-3 py-2.5 text-[12.5px]">
          <AlertTriangle
            className="size-3.5 text-[color:var(--paper-minor)]"
            aria-hidden="true"
          />
          <strong className="text-paper-ink">
            The public page still shows the last published snapshot.
          </strong>
          Draft edits stay private until you publish changes.
        </div>
      ) : null}

      {report.scope_stale ? (
        <Alert variant="destructive">
          <AlertTitle>The experiment task list changed</AlertTitle>
          <AlertDescription>
            Sync QA, review the updated task sections, and then publish again.
          </AlertDescription>
        </Alert>
      ) : null}

      {canEdit || report.new_item_count > 0 ? (
        <div className="border-paper-line bg-paper-surface text-paper-ink-2 flex flex-wrap items-center gap-2.5 rounded-lg border px-3 py-2.5 text-[12.5px]">
          <RefreshCw
            className="text-paper-running size-3.5"
            aria-hidden="true"
          />
          <span>
            {report.new_item_count > 0
              ? `${report.new_item_count} new QA check${report.new_item_count === 1 ? " is" : "s are"} ready to add.`
              : "Refresh task sections and completed QA checks."}
          </span>
          {canEdit ? (
            <Button
              type="submit"
              form="experiment-qa-draft"
              formAction={syncAction}
              variant="outline"
              size="sm"
              className="ml-auto h-7"
            >
              Sync QA
            </Button>
          ) : (
            <span className="text-paper-ink-3 ml-auto text-[11px]">
              Ask an admin to sync it.
            </span>
          )}
        </div>
      ) : null}

      <ExperimentQaGlancePanel tasks={includedTasks} />

      <form
        id="experiment-qa-draft"
        action={canEdit ? saveAction : undefined}
        className="space-y-4"
      >
        <input
          type="hidden"
          name="draft_version"
          value={report.draft_version}
        />
        <fieldset
          disabled={!canEdit}
          className="m-0 grid gap-4 border-0 p-0 disabled:opacity-90"
        >
          <section className="border-paper-line bg-paper-surface rounded-[10px] border">
            <div className="border-paper-line border-b px-3 py-2.5">
              <h2 className="text-paper-ink-3 font-mono text-[10px] font-semibold tracking-[0.09em] uppercase">
                Report fields
              </h2>
            </div>
            <div className="grid gap-3 p-3 md:grid-cols-2">
              <label className="grid gap-1 md:col-span-2">
                <span className="text-paper-ink-3 font-mono text-[10px] font-semibold tracking-[0.09em] uppercase">
                  Title
                </span>
                <Input name="title" defaultValue={report.title} required />
              </label>
              <label className="grid gap-1">
                <span className="text-paper-ink-3 font-mono text-[10px] font-semibold tracking-[0.09em] uppercase">
                  Customer summary
                </span>
                <Textarea
                  name="summary"
                  defaultValue={report.summary}
                  rows={4}
                />
              </label>
              <label className="grid gap-1">
                <span className="text-paper-ink-3 font-mono text-[10px] font-semibold tracking-[0.09em] uppercase">
                  Conclusion
                </span>
                <Textarea
                  name="conclusion"
                  defaultValue={report.conclusion}
                  rows={4}
                />
              </label>
              <label className="grid gap-1">
                <span className="text-paper-ink-3 font-mono text-[10px] font-semibold tracking-[0.09em] uppercase">
                  Customer note
                </span>
                <Input
                  name="customer_note"
                  defaultValue={report.customer_note ?? ""}
                />
              </label>
              <label className="grid gap-1">
                <span className="text-paper-ink-3 inline-flex items-center gap-1 font-mono text-[10px] font-semibold tracking-[0.09em] uppercase">
                  <Pencil className="size-3" aria-hidden="true" /> Internal note
                </span>
                <Input
                  name="internal_note"
                  defaultValue={report.internal_note ?? ""}
                  className="bg-paper-surface-2 border-dashed"
                />
              </label>
            </div>
          </section>

          <section className="border-paper-line bg-paper-surface overflow-hidden rounded-[10px] border">
            <div className="border-paper-line flex flex-wrap items-center gap-2 border-b px-3 py-2.5">
              <h2 className="text-paper-ink-3 font-mono text-[10px] font-semibold tracking-[0.09em] uppercase">
                QA by task
              </h2>
              <span className="text-paper-ink-3 font-mono text-[11px]">
                {includedTasks.length} of {sortedTasks.length} tasks public
              </span>
            </div>

            {sortedTasks.map((task, taskIndex) => {
              const sortedItems = [...task.items].sort(
                (a, b) => a.sort_order - b.sort_order
              );
              return (
                <section
                  key={task.id}
                  className={taskIndex > 0 ? "border-paper-line border-t" : ""}
                >
                  <input type="hidden" name="task_id" value={task.id} />
                  <div
                    className={`border-paper-line-2 bg-paper-surface-2 grid gap-2 border-b px-3 py-2.5 sm:grid-cols-[auto_72px_1fr] sm:items-center ${
                      task.is_visible ? "" : "opacity-60"
                    }`}
                  >
                    <VisibilityCheckbox
                      name={experimentQaFormField(
                        "task",
                        task.id,
                        "is_visible"
                      )}
                      checked={task.is_visible}
                      label={`Include ${task.name} in public QA`}
                    />
                    <label className="text-paper-ink-3 flex items-center gap-1 font-mono text-[10px]">
                      Order
                      <input
                        type="number"
                        min="0"
                        name={experimentQaFormField(
                          "task",
                          task.id,
                          "sort_order"
                        )}
                        defaultValue={task.sort_order}
                        className="border-paper-line bg-paper-surface text-paper-ink h-7 w-12 rounded border px-1.5"
                      />
                    </label>
                    <Input
                      name={experimentQaFormField("task", task.id, "name")}
                      defaultValue={task.name}
                      aria-label={`Public name for ${task.name}`}
                      required
                      className="hover:border-paper-line focus:border-paper-line h-8 border-transparent bg-transparent font-mono text-[13px] font-semibold"
                    />
                  </div>
                  <div className="border-paper-line-2 grid gap-2 border-b px-3 py-2.5 md:grid-cols-2">
                    <label className="grid gap-1">
                      <span className="text-paper-ink-3 font-mono text-[10px] uppercase">
                        Customer summary
                      </span>
                      <Textarea
                        name={experimentQaFormField("task", task.id, "summary")}
                        defaultValue={task.summary ?? ""}
                        rows={2}
                      />
                    </label>
                    <label className="grid gap-1">
                      <span className="text-paper-ink-3 font-mono text-[10px] uppercase">
                        Internal note
                      </span>
                      <Textarea
                        name={experimentQaFormField(
                          "task",
                          task.id,
                          "internal_note"
                        )}
                        defaultValue={task.internal_note ?? ""}
                        rows={2}
                        className="bg-paper-surface-2 border-dashed"
                      />
                    </label>
                  </div>

                  <div className="overflow-x-auto">
                    <table className="w-full min-w-[760px] border-collapse text-[12px]">
                      <thead>
                        <tr className="border-paper-line-2 bg-paper-surface-2 text-paper-ink-3 border-b text-left font-mono text-[10px] tracking-[0.08em] uppercase">
                          <th className="w-16 px-3 py-2 font-semibold">
                            Public
                          </th>
                          <th className="w-28 px-3 py-2 font-semibold">Type</th>
                          <th className="w-32 px-3 py-2 font-semibold">
                            Result
                          </th>
                          <th className="px-3 py-2 font-semibold">Finding</th>
                          <th className="w-28 px-3 py-2 font-semibold">
                            Source
                          </th>
                        </tr>
                      </thead>
                      <tbody className="divide-paper-line-2 divide-y">
                        {sortedItems.map((item, itemIndex) => (
                          <tr
                            key={item.id}
                            className={item.is_visible ? "" : "opacity-60"}
                          >
                            <td className="px-3 py-2 align-top">
                              <input
                                type="hidden"
                                name="item_id"
                                value={item.id}
                              />
                              <div className="flex items-center gap-2">
                                <VisibilityCheckbox
                                  name={experimentQaFormField(
                                    "item",
                                    item.id,
                                    "is_visible"
                                  )}
                                  checked={item.is_visible}
                                  label={`Include ${item.title || "QA finding"} publicly`}
                                />
                                <input
                                  type="number"
                                  min="0"
                                  name={experimentQaFormField(
                                    "item",
                                    item.id,
                                    "sort_order"
                                  )}
                                  defaultValue={item.sort_order ?? itemIndex}
                                  aria-label={`Order for ${item.title || "QA finding"}`}
                                  className="border-paper-line bg-paper-surface text-paper-ink h-7 w-11 rounded border px-1 text-center font-mono text-[10px]"
                                />
                              </div>
                            </td>
                            <td className="text-paper-ink-2 px-3 py-2 align-top font-mono text-[11px]">
                              {EXPERIMENT_QA_SOURCE_LABEL[item.source_type]}
                            </td>
                            <td className="px-3 py-2 align-top">
                              <ExperimentQaResultChip
                                signal={experimentQaSignal(item)}
                              />
                            </td>
                            <td className="px-3 py-2 align-top">
                              <details className="group">
                                <summary className="text-paper-ink cursor-pointer list-none font-medium select-none">
                                  <span
                                    className="text-paper-ink-3 mr-1.5 inline-block text-[9px] transition-transform group-open:rotate-90"
                                    aria-hidden="true"
                                  >
                                    &#9654;
                                  </span>
                                  {item.title ||
                                    item.source_title ||
                                    "QA finding"}
                                </summary>
                                <div className="border-paper-line bg-paper-surface-2 mt-3 grid gap-3 rounded-lg border p-3">
                                  <label className="grid gap-1">
                                    <span className="text-paper-ink-3 font-mono text-[10px] uppercase">
                                      Public title
                                    </span>
                                    <Input
                                      name={experimentQaFormField(
                                        "item",
                                        item.id,
                                        "title"
                                      )}
                                      defaultValue={item.title ?? ""}
                                      required
                                    />
                                  </label>
                                  <label className="grid gap-1">
                                    <span className="text-paper-ink-3 font-mono text-[10px] uppercase">
                                      Public summary
                                    </span>
                                    <Textarea
                                      name={experimentQaFormField(
                                        "item",
                                        item.id,
                                        "summary"
                                      )}
                                      defaultValue={item.summary ?? ""}
                                      rows={3}
                                    />
                                  </label>
                                  <label className="grid gap-1">
                                    <span className="text-paper-ink-3 font-mono text-[10px] uppercase">
                                      Recommendation
                                    </span>
                                    <Textarea
                                      name={experimentQaFormField(
                                        "item",
                                        item.id,
                                        "recommendation"
                                      )}
                                      defaultValue={item.recommendation ?? ""}
                                      rows={2}
                                    />
                                  </label>
                                  <label className="grid gap-1">
                                    <span className="text-paper-ink-3 font-mono text-[10px] uppercase">
                                      Public evidence
                                    </span>
                                    <Textarea
                                      name={experimentQaFormField(
                                        "item",
                                        item.id,
                                        "evidence"
                                      )}
                                      defaultValue={item.evidence ?? ""}
                                      rows={3}
                                      placeholder="Optional customer-safe evidence"
                                    />
                                  </label>
                                  <div className="grid gap-3 md:grid-cols-2">
                                    <label className="grid gap-1">
                                      <span className="text-paper-ink-3 font-mono text-[10px] uppercase">
                                        Customer note
                                      </span>
                                      <Input
                                        name={experimentQaFormField(
                                          "item",
                                          item.id,
                                          "customer_note"
                                        )}
                                        defaultValue={item.customer_note ?? ""}
                                      />
                                    </label>
                                    <label className="grid gap-1">
                                      <span className="text-paper-ink-3 font-mono text-[10px] uppercase">
                                        Internal note
                                      </span>
                                      <Input
                                        name={experimentQaFormField(
                                          "item",
                                          item.id,
                                          "internal_note"
                                        )}
                                        defaultValue={item.internal_note ?? ""}
                                        className="bg-paper-surface border-dashed"
                                      />
                                    </label>
                                  </div>
                                  <label className="border-paper-line bg-paper-surface text-paper-ink-2 flex items-start gap-2 rounded border p-2 text-[11px]">
                                    <input
                                      type="checkbox"
                                      name={experimentQaFormField(
                                        "item",
                                        item.id,
                                        "include_evidence"
                                      )}
                                      value="true"
                                      defaultChecked={item.include_evidence}
                                      className="mt-0.5 size-4 accent-[color:var(--paper-pass)]"
                                    />
                                    Include the customer-safe evidence and file
                                    location in the public snapshot
                                  </label>
                                  <details className="border-paper-line border-l-2 pl-3">
                                    <summary className="text-paper-ink-3 cursor-pointer font-mono text-[10px]">
                                      Private source context
                                    </summary>
                                    <div className="text-paper-ink-3 mt-2 grid gap-1.5 font-mono text-[10.5px] whitespace-pre-wrap">
                                      {item.source_title ? (
                                        <p>{item.source_title}</p>
                                      ) : null}
                                      {item.source_summary ? (
                                        <p>{item.source_summary}</p>
                                      ) : null}
                                      {item.source_recommendation ? (
                                        <p>Fix: {item.source_recommendation}</p>
                                      ) : null}
                                      {item.source_evidence ? (
                                        <p>{item.source_evidence}</p>
                                      ) : null}
                                    </div>
                                  </details>
                                </div>
                              </details>
                            </td>
                            <td className="text-paper-ink-3 px-3 py-2 align-top font-mono text-[10px]">
                              {item.source_label || item.source_type}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </section>
              );
            })}
          </section>
        </fieldset>
      </form>
    </div>
  );
}
