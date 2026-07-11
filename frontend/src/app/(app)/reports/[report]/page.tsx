import { ReportDetailClient } from "./report-detail-client";

export default async function ReportDetailPage({
  params,
}: {
  params: Promise<{ report: string }>;
}) {
  const { report } = await params;
  return <ReportDetailClient reportId={report} />;
}
