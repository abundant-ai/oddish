import type { Metadata } from "next";
import { decodeExperimentRouteParam } from "@/lib/utils";
import { ExperimentPageClient } from "@/components/experiment-page-client";

export async function generateMetadata({
  params,
}: {
  params: Promise<{ experiment: string }>;
}): Promise<Metadata> {
  const { experiment } = await params;
  const experimentId = decodeExperimentRouteParam(experiment ?? "");
  // The route metadata deliberately avoids a second backend request. The
  // bounded /open resource supplies the human-readable name to the page.
  const title = experimentId
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
// fetches the bounded /open resource without delaying the route shell,
// and keeping a server copy and a client copy of the same
// data caused staleness conflicts between them. The page shows its
// skeleton immediately and the client fetches the data once.
export default async function ExperimentDetailPage({
  params,
}: {
  params: Promise<{ experiment: string }>;
}) {
  const { experiment } = await params;
  const experimentId = decodeExperimentRouteParam(experiment ?? "");

  return (
    <ExperimentPageClient access={{ kind: "member", experimentId }} />
  );
}
