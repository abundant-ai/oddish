import type { Metadata } from "next";
import { auth } from "@clerk/nextjs/server";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { ExperimentSectionTabs } from "@/components/experiment-qa/experiment-section-tabs";
import {
  ExperimentQaEmptyState,
  ExperimentQaPrivateEditor,
} from "@/components/experiment-qa/private-editor";
import { ExperimentQaPublicReport } from "@/components/experiment-qa/public-report";
import { ExperimentQaPublishConfirmation } from "@/components/experiment-qa/publish-confirmation";
import {
  ExperimentQaRequestError,
  getExperimentQa,
  getExperimentQaPreview,
  getExperimentQaShareInfo,
} from "@/lib/experiment-qa-server";
import {
  decodeExperimentRouteParam,
  encodeExperimentRouteParam,
} from "@/lib/utils";
import { isOrgAdminRole } from "@/lib/org-roles";
import {
  createExperimentQaAction,
  confirmPublishExperimentQaAction,
  confirmUnpublishExperimentQaAction,
  previewExperimentQaAction,
  publishExperimentQaAction,
  saveExperimentQaAction,
  syncExperimentQaAction,
  unpublishExperimentQaAction,
} from "./actions";

export const metadata: Metadata = {
  title: "Experiment QA · Oddish",
  description: "Curate and publish QA for one Oddish experiment.",
};

type SearchParams = Record<string, string | string[] | undefined>;

function firstParam(params: SearchParams, name: string): string | null {
  const value = params[name];
  if (Array.isArray(value)) return value[0] ?? null;
  return value ?? null;
}

function noticeFrom(params: SearchParams) {
  const error = firstParam(params, "error");
  if (error) {
    return {
      tone: "error" as const,
      title: "QA was not saved",
      detail: error,
    };
  }
  if (firstParam(params, "created") === "1") {
    return {
      tone: "success" as const,
      title: "QA draft created",
      detail: "Completed experiment checks were gathered into a private draft.",
    };
  }
  if (firstParam(params, "synced") === "1") {
    return {
      tone: "success" as const,
      title: "QA draft synced",
      detail:
        "New completed checks were added. The public snapshot did not change.",
    };
  }
  if (firstParam(params, "saved") === "1") {
    return {
      tone: "success" as const,
      title: "Draft saved",
      detail: "Your private QA changes were saved.",
    };
  }
  if (firstParam(params, "published") === "1") {
    return {
      tone: "success" as const,
      title: "QA published",
      detail:
        "The new immutable public snapshot is live. Copy its link from the header.",
    };
  }
  if (firstParam(params, "unpublished") === "1") {
    return {
      tone: "success" as const,
      title: "QA unpublished",
      detail: "The old public QA link is now disabled.",
    };
  }
  return null;
}

export default async function ExperimentQaPage({
  params,
  searchParams,
}: {
  params: Promise<{ experiment: string }>;
  searchParams: Promise<SearchParams>;
}) {
  const [{ experiment }, query, authObject] = await Promise.all([
    params,
    searchParams,
    auth(),
  ]);
  const experimentId = decodeExperimentRouteParam(experiment ?? "");
  const encodedExperiment = encodeExperimentRouteParam(experimentId);
  const experimentHref = `/experiments/${encodedExperiment}`;
  const qaHref = `${experimentHref}/qa`;
  const previewRequested = firstParam(query, "preview") === "1";
  const canEdit = isOrgAdminRole(authObject.orgRole);

  if (!experimentId) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Missing experiment</AlertTitle>
        <AlertDescription>
          Select an experiment before opening QA.
        </AlertDescription>
      </Alert>
    );
  }

  if (previewRequested) {
    try {
      const preview = await getExperimentQaPreview(experimentId);
      return (
        <div className="space-y-4">
          <ExperimentSectionTabs
            active="qa"
            experimentHref={experimentHref}
            qaHref={qaHref}
          />
          <ExperimentQaPublicReport
            report={preview}
            experimentHref={qaHref}
            preview
          />
        </div>
      );
    } catch (error) {
      const detail =
        error instanceof Error
          ? error.message
          : "The preview could not be built.";
      return (
        <div className="space-y-4">
          <ExperimentSectionTabs
            active="qa"
            experimentHref={experimentHref}
            qaHref={qaHref}
          />
          <Alert variant="destructive">
            <AlertTitle>Preview unavailable</AlertTitle>
            <AlertDescription>{detail}</AlertDescription>
          </Alert>
        </div>
      );
    }
  }

  try {
    const [report, shareInfo] = await Promise.all([
      getExperimentQa(experimentId),
      getExperimentQaShareInfo(experimentId),
    ]);
    const experimentName = shareInfo?.name || experimentId;
    const confirmPublish = firstParam(query, "confirm_publish") === "1";
    const confirmUnpublish = firstParam(query, "confirm_unpublish") === "1";

    return (
      <div className="space-y-4">
        <ExperimentSectionTabs
          active="qa"
          experimentHref={experimentHref}
          qaHref={qaHref}
        />
        {report && canEdit && (confirmPublish || confirmUnpublish) ? (
          <ExperimentQaPublishConfirmation
            mode={confirmPublish ? "publish" : "unpublish"}
            report={report}
            cancelHref={qaHref}
            confirmAction={
              confirmPublish
                ? publishExperimentQaAction.bind(null, experimentId)
                : unpublishExperimentQaAction.bind(null, experimentId)
            }
            canPublish={Boolean(shareInfo?.public_token)}
            error={firstParam(query, "error")}
          />
        ) : report ? (
          <ExperimentQaPrivateEditor
            report={report}
            experimentName={experimentName}
            experimentPublicToken={shareInfo?.public_token ?? null}
            canEdit={canEdit}
            previewHref={`${qaHref}?preview=1`}
            saveAction={saveExperimentQaAction.bind(null, experimentId)}
            previewAction={previewExperimentQaAction.bind(null, experimentId)}
            publishAction={confirmPublishExperimentQaAction.bind(
              null,
              experimentId
            )}
            unpublishAction={confirmUnpublishExperimentQaAction.bind(
              null,
              experimentId
            )}
            syncAction={syncExperimentQaAction.bind(null, experimentId)}
            notice={noticeFrom(query)}
          />
        ) : (
          <ExperimentQaEmptyState
            experimentName={experimentName}
            canEdit={canEdit}
            createAction={createExperimentQaAction.bind(null, experimentId)}
          />
        )}
      </div>
    );
  } catch (error) {
    const detail =
      error instanceof ExperimentQaRequestError || error instanceof Error
        ? error.message
        : "Experiment QA could not be loaded.";
    return (
      <div className="space-y-4">
        <ExperimentSectionTabs
          active="qa"
          experimentHref={experimentHref}
          qaHref={qaHref}
        />
        <Alert variant="destructive">
          <AlertTitle>Failed to load experiment QA</AlertTitle>
          <AlertDescription>{detail}</AlertDescription>
        </Alert>
      </div>
    );
  }
}
