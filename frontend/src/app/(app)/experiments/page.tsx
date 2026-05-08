import type { Metadata } from "next";
import { ExperimentsListClient } from "./experiments-list-client";

export const metadata: Metadata = {
  title: "Experiments · Oddish",
};

export default function ExperimentsListPage() {
  return <ExperimentsListClient />;
}
