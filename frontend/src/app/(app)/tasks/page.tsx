import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import type { TaskBrowseResponse } from "@/lib/types";
import { TasksPageClient } from "./tasks-client";

async function getInitialTaskBrowseData(): Promise<TaskBrowseResponse | null> {
  try {
    const authObj = await auth();
    if (!authObj?.userId) {
      return null;
    }

    const token = await getClerkToken(authObj.getToken);
    if (!token) {
      return null;
    }

    const url = getBackendUrl("tasks/browse", "", {
      limit: "25",
      offset: "0",
    });
    const response = await fetch(url, {
      cache: "no-store",
      headers: getAuthHeaders(token),
    });
    if (!response.ok) {
      console.error(
        `[tasks/page] Failed initial task browser fetch: ${response.status}`,
      );
      return null;
    }

    return (await response.json()) as TaskBrowseResponse;
  } catch (error) {
    console.error("[tasks/page] Initial task browser fetch failed", error);
    return null;
  }
}

export default async function TasksPage() {
  // SSR fetch is for the unfiltered first page; once URL params land on
  // the client, it refetches against the matching key. URL state is the
  // source of truth for everything else.
  const initialData = await getInitialTaskBrowseData();
  return <TasksPageClient initialData={initialData} />;
}
