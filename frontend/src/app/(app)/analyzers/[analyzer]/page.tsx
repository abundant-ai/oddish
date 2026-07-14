import { AnalyzerDetailClient } from "./analyzer-detail-client";

export default async function AnalyzerDetailPage({
  params,
}: {
  params: Promise<{ analyzer: string }>;
}) {
  const { analyzer } = await params;
  return <AnalyzerDetailClient analyzerId={analyzer} />;
}
