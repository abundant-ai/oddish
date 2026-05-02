import { JobDetailClient } from "./job-detail-client";

export default async function JobDetailPage({
  params,
}: {
  params: Promise<{ job: string }>;
}) {
  const { job } = await params;
  return <JobDetailClient jobId={job} />;
}
