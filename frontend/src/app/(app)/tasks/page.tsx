import { TasksPageClient } from "./tasks-client";

export default async function TasksPage({
  searchParams,
}: {
  searchParams?: Promise<{ query?: string | string[] }>;
}) {
  const params = await searchParams;
  const queryParam = params?.query;
  const initialQuery = Array.isArray(queryParam)
    ? (queryParam[0] ?? "")
    : (queryParam ?? "");
  // No SSR data fetch -- the client's useSWR fetches on mount when no
  // fallbackData is provided, so the shell paints instantly.
  return <TasksPageClient initialData={null} initialQuery={initialQuery} />;
}
