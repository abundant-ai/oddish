"use client";

import { useEffect, useMemo, useState } from "react";
import type { Trial } from "@/lib/types";
import {
  embeddedCtrfSummary,
  parseCtrfReport,
  type CtrfSummary,
} from "@/lib/verifier-results";

const TERMINAL_STATUSES = ["success", "failed", "cancelled", "skipped"];

function encodeFilePath(path: string): string {
  return path.split("/").map(encodeURIComponent).join("/");
}

export function useVerifierSummary(
  trial: Trial | null,
  apiBaseUrl: string,
  enabled: boolean,
): CtrfSummary | null {
  const result = trial?.result;
  const embedded = useMemo(() => embeddedCtrfSummary(result), [result]);
  const trialId = trial?.id ?? null;
  const shouldLoad =
    enabled &&
    trial !== null &&
    embedded === null &&
    TERMINAL_STATUSES.includes(trial.status);
  const artifactKey = `${apiBaseUrl}\0${trialId ?? ""}`;
  const [artifact, setArtifact] = useState<{
    key: string;
    summary: CtrfSummary | null;
  }>({ key: artifactKey, summary: null });

  useEffect(() => {
    setArtifact({ key: artifactKey, summary: null });
    if (!shouldLoad || trialId === null) return;

    const controller = new AbortController();
    const filesBase = `${apiBaseUrl.replace(/\/$/, "")}/trials/${encodeURIComponent(trialId)}/files`;

    async function loadArtifactSummary() {
      let summary: CtrfSummary | null = null;
      try {
        const listingResponse = await fetch(
          `${filesBase}?recursive=true&presign=false&limit=1000`,
          { signal: controller.signal },
        );
        if (!listingResponse.ok) return;
        const listing = (await listingResponse.json()) as {
          files?: { path?: string }[];
        };
        const reportPath = listing.files
          ?.map((file) => file.path)
          .find(
            (path): path is string =>
              typeof path === "string" &&
              (path === "verifier/ctrf.json" ||
                path.endsWith("/verifier/ctrf.json")),
          );
        if (!reportPath) return;

        const reportResponse = await fetch(
          `${filesBase}/${encodeFilePath(reportPath)}`,
          { signal: controller.signal },
        );
        if (!reportResponse.ok) return;
        summary = parseCtrfReport(await reportResponse.json());
      } catch {
        summary = null;
      } finally {
        if (!controller.signal.aborted) {
          setArtifact({ key: artifactKey, summary });
        }
      }
    }

    void loadArtifactSummary();
    return () => controller.abort();
  }, [apiBaseUrl, artifactKey, shouldLoad, trialId]);

  if (embedded) return embedded;
  return artifact.key === artifactKey ? artifact.summary : null;
}
