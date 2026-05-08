import type { Metadata } from "next";
import { JobsPageClient } from "./jobs-client";

export const metadata: Metadata = {
  title: "Jobs · Oddish",
  description: "Recently launched batches of trials and what's currently running.",
};

export default function JobsPage() {
  return <JobsPageClient />;
}
