"use client";

import { useEffect, useRef, useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import { useAuth } from "@clerk/nextjs";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { ExperimentShareButton } from "@/components/experiment-share-button";
import { ExperimentCellMatrix } from "@/components/experiment-cell-matrix";
import { Pencil } from "lucide-react";
import { fetcher } from "@/lib/api";
import { encodeExperimentRouteParam } from "@/lib/utils";
import type { ResolvedExperiment } from "@/lib/types";

type ExperimentClientPageProps = {
  experimentId: string;
};

export function ExperimentClientPage({
  experimentId,
}: ExperimentClientPageProps) {
  const { orgRole } = useAuth();
  const { mutate: mutateKey } = useSWRConfig();

  const [isEditingName, setIsEditingName] = useState(false);
  const [nameDraft, setNameDraft] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);
  const [isSavingName, setIsSavingName] = useState(false);
  const [copiedExperimentName, setCopiedExperimentName] = useState(false);
  const copiedExperimentNameTimeoutRef = useRef<number | null>(null);

  const encodedId = experimentId
    ? encodeExperimentRouteParam(experimentId)
    : "";

  const resolvedUrl = experimentId
    ? `/api/experiments/${encodedId}/resolved`
    : null;
  const { data: resolved, isLoading: isLoadingResolved } =
    useSWR<ResolvedExperiment>(resolvedUrl, fetcher, {
      refreshInterval: 60_000,
      revalidateOnFocus: false,
    });

  const experimentName = resolved?.experiment_name ?? "";
  // While the resolved fetch is in flight, show a placeholder rather
  // than flashing the raw experiment id.
  const displayName = experimentName
    ? experimentName
    : isLoadingResolved
      ? "…"
      : experimentId || "Experiment";
  const initialName = experimentName;
  const canManageExperimentShare =
    orgRole === "org:admin" || orgRole === "org:owner";

  useEffect(() => {
    if (!isEditingName) {
      setNameDraft(initialName);
      setNameError(null);
    }
  }, [initialName, isEditingName]);

  useEffect(() => {
    setCopiedExperimentName(false);
    if (copiedExperimentNameTimeoutRef.current !== null) {
      window.clearTimeout(copiedExperimentNameTimeoutRef.current);
      copiedExperimentNameTimeoutRef.current = null;
    }
  }, [displayName]);

  useEffect(() => {
    return () => {
      if (copiedExperimentNameTimeoutRef.current !== null) {
        window.clearTimeout(copiedExperimentNameTimeoutRef.current);
      }
    };
  }, []);

  const handleRename = async () => {
    if (!experimentId) return;
    const nextName = nameDraft.trim();
    if (!nextName) {
      setNameError("Experiment name cannot be empty.");
      return;
    }

    setIsSavingName(true);
    setNameError(null);

    try {
      const res = await fetch(`/api/experiments/${encodedId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: nextName }),
      });

      if (!res.ok) {
        const errorData = await res.json().catch(() => ({}));
        throw new Error(
          errorData.detail || errorData.error || "Failed to rename experiment",
        );
      }

      setIsEditingName(false);
      // Reflect the rename across every cache that surfaces the name:
      // the resolved view (matrix), the experiments index, and any
      // share-token-driven view that might be cached client-side.
      await Promise.all([
        mutateKey(
          `/api/experiments/${encodedId}/resolved`,
          (current: unknown) => {
            if (!current || typeof current !== "object") return current;
            return { ...(current as object), experiment_name: nextName };
          },
          { revalidate: true },
        ),
        mutateKey(
          (key: unknown) =>
            typeof key === "string" && key.startsWith("/api/experiments?"),
          undefined,
          { revalidate: true },
        ),
      ]);
    } catch (err) {
      setNameError(err instanceof Error ? err.message : "Rename failed");
    } finally {
      setIsSavingName(false);
    }
  };

  const handleCopyExperimentName = async () => {
    await navigator.clipboard.writeText(displayName);
    setCopiedExperimentName(true);
    if (copiedExperimentNameTimeoutRef.current !== null) {
      window.clearTimeout(copiedExperimentNameTimeoutRef.current);
    }
    copiedExperimentNameTimeoutRef.current = window.setTimeout(() => {
      setCopiedExperimentName(false);
      copiedExperimentNameTimeoutRef.current = null;
    }, 2000);
  };

  if (!experimentId) {
    return (
      <Alert>
        <AlertTitle>Missing experiment</AlertTitle>
        <AlertDescription>
          Select an experiment from the list.
        </AlertDescription>
      </Alert>
    );
  }

  return (
    <div className="space-y-4 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          {isEditingName ? (
            <div className="flex flex-wrap items-center gap-2">
              <Input
                value={nameDraft}
                onChange={(event) => setNameDraft(event.target.value)}
                className="h-10 w-[320px] font-mono text-[22px] font-semibold tracking-[-0.02em]"
                placeholder="Experiment name"
              />
              <Button
                type="button"
                size="sm"
                className="h-8"
                onClick={handleRename}
                disabled={isSavingName}
              >
                {isSavingName ? "Saving…" : "Save"}
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-8"
                onClick={() => setIsEditingName(false)}
                disabled={isSavingName}
              >
                Cancel
              </Button>
            </div>
          ) : (
            <div className="flex min-w-0 items-center gap-2">
              <button
                type="button"
                onClick={handleCopyExperimentName}
                className="min-w-0 max-w-full truncate text-left font-mono text-[26px] font-semibold leading-[1.25] tracking-[-0.02em] hover:text-muted-foreground"
                aria-label={`Copy experiment name ${displayName}`}
                title={
                  copiedExperimentName
                    ? "Copied"
                    : "Click to copy experiment name"
                }
              >
                {displayName}
              </button>
              {copiedExperimentName ? (
                <span
                  aria-live="polite"
                  className="font-mono text-[11px] text-muted-foreground"
                >
                  copied
                </span>
              ) : null}
              <Button
                type="button"
                variant="ghost"
                size="icon"
                onClick={() => setIsEditingName(true)}
                className="h-7 w-7 rounded-sm"
                aria-label="Rename experiment"
                title="Rename experiment"
              >
                <Pencil className="h-3.5 w-3.5" />
              </Button>
            </div>
          )}
          {nameError ? (
            <p className="mt-2 text-xs text-destructive">{nameError}</p>
          ) : null}
        </div>
        <ExperimentShareButton
          experimentId={experimentId}
          canManageShare={canManageExperimentShare}
        />
      </div>

      <ExperimentCellMatrix
        experimentId={experimentId}
        canEdit={canManageExperimentShare}
      />
    </div>
  );
}
