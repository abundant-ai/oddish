import type { Metadata } from "next";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { ExperimentSectionTabs } from "@/components/experiment-qa/experiment-section-tabs";
import { ExperimentQaPublicReport } from "@/components/experiment-qa/public-report";
import { ShareNav } from "@/components/share-nav";
import { buildPublicQaHref } from "@/lib/experiment-qa";
import { getPublicExperimentQa } from "@/lib/experiment-qa-server";

export const metadata: Metadata = {
  title: "Experiment QA · Oddish",
  description: "Read the published QA snapshot for an Oddish experiment.",
};

export default async function PublicExperimentQaPage({
  params,
  searchParams,
}: {
  params: Promise<{ token: string }>;
  searchParams: Promise<{ t?: string | string[] }>;
}) {
  const [{ token }, query] = await Promise.all([params, searchParams]);
  const tokenParam = Array.isArray(query.t) ? query.t[0] : query.t;
  const experimentHref = `/share/${encodeURIComponent(token)}`;
  const qaHref = tokenParam ? buildPublicQaHref(token, tokenParam) : null;

  let report = null;
  let error: string | null = null;
  if (!tokenParam) {
    error = "This QA link is missing its QA token.";
  } else {
    try {
      report = await getPublicExperimentQa(token, tokenParam);
    } catch (caught) {
      error =
        caught instanceof Error
          ? caught.message
          : "This QA link is not available.";
    }
  }

  return (
    <>
      <ShareNav />
      <main className="mx-auto w-full max-w-(--breakpoint-2xl) px-4 py-4">
        <div className="space-y-4">
          <ExperimentSectionTabs
            active="qa"
            experimentHref={experimentHref}
            qaHref={qaHref}
            publicView
          />
          {report ? (
            <ExperimentQaPublicReport
              report={report}
              experimentHref={experimentHref}
            />
          ) : (
            <Alert variant="destructive">
              <AlertTitle>Public QA is unavailable</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
        </div>
      </main>
    </>
  );
}
