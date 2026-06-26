import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import type { TagListResponse } from "@/lib/types";
import { TagsPageClient } from "./tags-client";

async function getInitialTags(): Promise<TagListResponse | null> {
  try {
    const authObj = await auth();
    if (!authObj?.userId) return null;

    const token = await getClerkToken(authObj.getToken);
    if (!token) return null;

    const url = getBackendUrl("tags");
    const response = await fetch(url, {
      cache: "no-store",
      headers: getAuthHeaders(token),
    });
    if (!response.ok) {
      console.error(
        `[tags/page] Failed initial tags fetch: ${response.status}`,
      );
      return null;
    }
    return (await response.json()) as TagListResponse;
  } catch (error) {
    console.error("[tags/page] Initial tags fetch failed", error);
    return null;
  }
}

export default async function TagsPage() {
  const initialData = await getInitialTags();
  return <TagsPageClient initialData={initialData} />;
}
