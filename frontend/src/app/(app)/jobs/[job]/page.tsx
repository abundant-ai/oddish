import type { Metadata } from "next";
import { JobDetailClient } from "./job-detail-client";

type Params = Promise<{ job: string }>;

export async function generateMetadata({
  params,
}: {
  params: Params;
}): Promise<Metadata> {
  const { job } = await params;
  return {
    title: `Job ${job} · Oddish`,
    description: "Trials produced by this job.",
  };
}

export default async function JobDetailPage({ params }: { params: Params }) {
  const { job } = await params;
  return <JobDetailClient jobId={job} />;
}
