import type { Metadata } from "next";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import { decodeExperimentRouteParam } from "@/lib/utils";
import { ExperimentClientPage } from "./experiment-client";

async function getExperimentName(experimentId: string): Promise<string | null> {
  try {
    const authObj = await auth();
    if (!authObj?.userId) return null;

    const token = await getClerkToken(authObj.getToken);
    if (!token) return null;

    const url = getBackendUrl(
      "experiments",
      `/${encodeURIComponent(experimentId)}/share`
    );
    const response = await fetch(url, {
      cache: "no-store",
      headers: getAuthHeaders(token),
      // This gives the title fetch 2.5 seconds. If the backend is slower
      // than that, the page uses the experiment id as the title instead
      // of waiting, so a slow backend cannot delay the page.
      signal: AbortSignal.timeout(2500),
    });
    if (!response.ok) {
      console.error(
        `[experiment/page] Failed experiment metadata fetch: ${response.status}`
      );
      return null;
    }

    const data = (await response.json()) as { name?: unknown };
    return typeof data.name === "string" && data.name.trim() ? data.name : null;
  } catch (error) {
    console.error("[experiment/page] Experiment metadata fetch failed", error);
    return null;
  }
}

export async function generateMetadata({
  params,
}: {
  params: Promise<{ experiment: string }>;
}): Promise<Metadata> {
  const { experiment } = await params;
  const experimentId = decodeExperimentRouteParam(experiment ?? "");
  const experimentName = experimentId
    ? await getExperimentName(experimentId)
    : null;
  const title = experimentName
    ? `${experimentName} · Oddish`
    : experimentId
      ? `Experiment ${experimentId} · Oddish`
      : "Experiment · Oddish";
  const description =
    "View trials, rewards, and task details for this Oddish experiment.";
  const image = "/oddish.png";
  return {
    title,
    description,
    openGraph: {
      type: "website",
      siteName: "Oddish",
      title,
      description,
      images: [{ url: image, alt: "Oddish" }],
    },
    twitter: {
      card: "summary",
      title,
      description,
      images: [image],
    },
  };
}

// This page deliberately fetches no data on the server. The browser
// fetches the same task-shells URL faster than the server render could
// stream it, and keeping a server copy and a client copy of the same
// data caused staleness conflicts between them. The page shows its
// skeleton immediately and the client fetches the data once.
export default async function ExperimentDetailPage({
  params,
}: {
  params: Promise<{ experiment: string }>;
}) {
  const { experiment } = await params;
  const experimentId = decodeExperimentRouteParam(experiment ?? "");

  return <ExperimentClientPage experimentId={experimentId} />;
}
