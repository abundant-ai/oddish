"use client";

import { useParams } from "next/navigation";
import { ExperimentPageClient } from "@/components/experiment-page-client";
import { Nav } from "@/components/nav";

export default function PublicDatasetPage() {
  const params = useParams();
  const token = Array.isArray(params.token) ? params.token[0] : params.token;

  return (
    <>
      <Nav />

      <main className="mx-auto w-full max-w-(--breakpoint-2xl) px-4 py-4">
        {token ? (
          <ExperimentPageClient access={{ kind: "public", token }} />
        ) : null}
      </main>
    </>
  );
}
