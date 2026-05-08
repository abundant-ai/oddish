import type { Metadata } from "next";
import { NewExperimentClient } from "./new-experiment-client";

export const metadata: Metadata = {
  title: "New experiment · Oddish",
  description: "Create a new experiment as a saved selection of task versions and agents.",
};

export default function NewExperimentPage() {
  return <NewExperimentClient />;
}
