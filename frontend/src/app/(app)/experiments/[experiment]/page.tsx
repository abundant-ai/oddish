import { decodeExperimentRouteParam } from "@/lib/utils";
import { ExperimentClientPage } from "./experiment-client";

export default async function ExperimentDetailPage({
  params,
}: {
  params: Promise<{ experiment: string }>;
}) {
  const { experiment } = await params;
  const experimentId = decodeExperimentRouteParam(experiment ?? "");

  // Avoid blocking route navigation on server-side tasks fetch.
  return <ExperimentClientPage experimentId={experimentId} />;
}
