"use client";

import { useAuth } from "@clerk/nextjs";
import { encodeFilePath } from "@/lib/file-path";
import { apiFetch } from "@/lib/api";

export const FILE_PREVIEW_BYTES = 100 * 1024;

export function useFileCacheScope(url: string) {
  const { userId, orgId } = useAuth();
  return url.startsWith("/api/public/")
    ? "public"
    : userId && orgId
      ? `${userId}:${orgId}`
      : null;
}

export function trialFilePreviewKey(
  scope: string | null,
  url: string,
  path: string,
  attempt: number,
  revision: string | null
) {
  return scope && revision
    ? (["trial-file-preview", scope, url, path, attempt, revision] as const)
    : null;
}

export async function fetchTrialFilePreview(
  key: NonNullable<ReturnType<typeof trialFilePreviewKey>>
) {
  const url = `${key[2]}/${encodeFilePath(key[3])}?indexed=true&attempt=${key[4]}&revision=${encodeURIComponent(key[5])}&max_bytes=${FILE_PREVIEW_BYTES}`;
  const response = await apiFetch(url, { signal: AbortSignal.timeout(15_000) });
  if (!response.ok)
    throw Object.assign(
      new Error(`Could not read file (HTTP ${response.status})`),
      {
        status: response.status,
      }
    );
  const bytes = await response.arrayBuffer();
  return {
    kind: "text" as const,
    content: new TextDecoder().decode(bytes),
    isTruncated: bytes.byteLength >= FILE_PREVIEW_BYTES,
    size: null,
    sourceHash: null,
  };
}
