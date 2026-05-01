import type { Metadata } from "next";
import { ExperimentsListClient } from "./experiments-list-client";

export const metadata: Metadata = {
  title: "Experiments · Oddish",
  description: "Saved selections of task versions and agents.",
};

export default function ExperimentsListPage() {
  return <ExperimentsListClient />;
}
