"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import {
  createExperimentQa,
  patchExperimentQa,
  publishExperimentQa,
  syncExperimentQa,
  unpublishExperimentQa,
} from "@/lib/experiment-qa-server";
import { experimentQaFormField } from "@/lib/experiment-qa";
import type { ExperimentQaPatch } from "@/lib/types";
import { encodeExperimentRouteParam } from "@/lib/utils";

function qaPath(experimentId: string): string {
  return `/experiments/${encodeExperimentRouteParam(experimentId)}/qa`;
}

function formString(formData: FormData, name: string): string {
  const value = formData.get(name);
  return typeof value === "string" ? value.trim() : "";
}

function nullableFormString(formData: FormData, name: string): string | null {
  return formString(formData, name) || null;
}

function formOrder(formData: FormData, name: string, fallback: number): number {
  const parsed = Number(formString(formData, name));
  return Number.isFinite(parsed) ? Math.max(0, Math.trunc(parsed)) : fallback;
}

function actionError(error: unknown): string {
  return error instanceof Error
    ? error.message
    : "Experiment QA could not be updated.";
}

function resultPath(
  experimentId: string,
  key: "created" | "saved" | "synced" | "published" | "unpublished"
) {
  return `${qaPath(experimentId)}?${key}=1`;
}

function errorPath(experimentId: string, error: string, query = "") {
  const separator = query ? "&" : "?";
  return `${qaPath(experimentId)}${query}${separator}error=${encodeURIComponent(error)}`;
}

export async function createExperimentQaAction(
  experimentId: string,
  _formData: FormData
) {
  let error: string | null = null;
  try {
    await createExperimentQa(experimentId);
  } catch (caught) {
    error = actionError(caught);
  }
  if (error) redirect(errorPath(experimentId, error));
  revalidatePath(qaPath(experimentId));
  redirect(resultPath(experimentId, "created"));
}

export async function syncExperimentQaAction(
  experimentId: string,
  formData: FormData
) {
  let error = await saveDraft(experimentId, formData);
  if (error) redirect(errorPath(experimentId, error));
  try {
    await syncExperimentQa(experimentId);
  } catch (caught) {
    error = actionError(caught);
  }
  if (error) redirect(errorPath(experimentId, error));
  revalidatePath(qaPath(experimentId));
  redirect(resultPath(experimentId, "synced"));
}

function patchFromForm(formData: FormData): ExperimentQaPatch {
  const taskIds = formData
    .getAll("task_id")
    .filter((value): value is string => typeof value === "string");
  const itemIds = formData
    .getAll("item_id")
    .filter((value): value is string => typeof value === "string");

  return {
    expected_draft_version: formOrder(formData, "draft_version", 0),
    title: formString(formData, "title"),
    summary: formString(formData, "summary"),
    conclusion: formString(formData, "conclusion"),
    customer_note: nullableFormString(formData, "customer_note"),
    internal_note: nullableFormString(formData, "internal_note"),
    tasks: taskIds.map((id, index) => ({
      id,
      name: formString(formData, experimentQaFormField("task", id, "name")),
      summary: nullableFormString(
        formData,
        experimentQaFormField("task", id, "summary")
      ),
      internal_note: nullableFormString(
        formData,
        experimentQaFormField("task", id, "internal_note")
      ),
      is_visible:
        formData.get(experimentQaFormField("task", id, "is_visible")) ===
        "true",
      sort_order: formOrder(
        formData,
        experimentQaFormField("task", id, "sort_order"),
        index
      ),
    })),
    items: itemIds.map((id, index) => ({
      id,
      title: formString(formData, experimentQaFormField("item", id, "title")),
      summary: nullableFormString(
        formData,
        experimentQaFormField("item", id, "summary")
      ),
      recommendation: nullableFormString(
        formData,
        experimentQaFormField("item", id, "recommendation")
      ),
      evidence: nullableFormString(
        formData,
        experimentQaFormField("item", id, "evidence")
      ),
      customer_note: nullableFormString(
        formData,
        experimentQaFormField("item", id, "customer_note")
      ),
      internal_note: nullableFormString(
        formData,
        experimentQaFormField("item", id, "internal_note")
      ),
      include_evidence:
        formData.get(experimentQaFormField("item", id, "include_evidence")) ===
        "true",
      is_visible:
        formData.get(experimentQaFormField("item", id, "is_visible")) ===
        "true",
      sort_order: formOrder(
        formData,
        experimentQaFormField("item", id, "sort_order"),
        index
      ),
    })),
  };
}

async function saveDraft(
  experimentId: string,
  formData: FormData
): Promise<string | null> {
  let error: string | null = null;
  try {
    await patchExperimentQa(experimentId, patchFromForm(formData));
  } catch (caught) {
    error = actionError(caught);
  }
  return error;
}

async function saveAndRedirect(
  experimentId: string,
  formData: FormData,
  destination: string
) {
  const error = await saveDraft(experimentId, formData);
  if (error) redirect(errorPath(experimentId, error));
  revalidatePath(qaPath(experimentId));
  redirect(destination);
}

export async function saveExperimentQaAction(
  experimentId: string,
  formData: FormData
) {
  await saveAndRedirect(
    experimentId,
    formData,
    resultPath(experimentId, "saved")
  );
}

export async function previewExperimentQaAction(
  experimentId: string,
  formData: FormData
) {
  await saveAndRedirect(
    experimentId,
    formData,
    `${qaPath(experimentId)}?preview=1`
  );
}

export async function confirmPublishExperimentQaAction(
  experimentId: string,
  formData: FormData
) {
  await saveAndRedirect(
    experimentId,
    formData,
    `${qaPath(experimentId)}?confirm_publish=1`
  );
}

export async function confirmUnpublishExperimentQaAction(
  experimentId: string,
  formData: FormData
) {
  await saveAndRedirect(
    experimentId,
    formData,
    `${qaPath(experimentId)}?confirm_unpublish=1`
  );
}

export async function publishExperimentQaAction(
  experimentId: string,
  formData: FormData
) {
  let error: string | null = null;
  try {
    await publishExperimentQa(
      experimentId,
      formOrder(formData, "expected_draft_version", -1),
      nullableFormString(formData, "expected_public_token")
    );
  } catch (caught) {
    error = actionError(caught);
  }
  if (error) {
    redirect(errorPath(experimentId, error, "?confirm_publish=1"));
  }
  revalidatePath(qaPath(experimentId));
  redirect(resultPath(experimentId, "published"));
}

export async function unpublishExperimentQaAction(
  experimentId: string,
  formData: FormData
) {
  let error: string | null = null;
  try {
    await unpublishExperimentQa(
      experimentId,
      formOrder(formData, "expected_draft_version", -1),
      formString(formData, "expected_public_token")
    );
  } catch (caught) {
    error = actionError(caught);
  }
  if (error) {
    redirect(errorPath(experimentId, error, "?confirm_unpublish=1"));
  }
  revalidatePath(qaPath(experimentId));
  redirect(resultPath(experimentId, "unpublished"));
}
