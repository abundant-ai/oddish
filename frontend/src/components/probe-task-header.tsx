"use client";

import useSWR from "swr";
import { useAuth } from "@clerk/nextjs";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8800";

type TaskInfo = {
  name?: string;
  task_path?: string;
};

export function ProbeTaskHeader({ taskId }: { taskId: string }) {
  const { getToken } = useAuth();

  const fetcher = async (url: string) => {
    let token: string | null = null;
    try {
      token = await getToken({ template: "oddish" });
    } catch {
      // template missing — fall back
    }
    if (!token) token = await getToken();
    const res = await fetch(url, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  };

  const { data } = useSWR<TaskInfo>(`${API_URL}/tasks/${taskId}`, fetcher);

  return (
    <div>
      <h1 className="text-2xl font-semibold">
        Probe run
        {data?.name ? (
          <>
            <span className="text-muted-foreground"> · </span>
            <span className="font-mono text-xl">{data.name}</span>
          </>
        ) : null}
      </h1>
      <p className="mt-1 text-sm text-muted-foreground">
        Submit a custom prompt prepended to this task&apos;s instruction. The
        agent runs in local Docker via Harbor.
      </p>
      <p className="mt-1 font-mono text-xs text-muted-foreground">
        task {taskId}
      </p>
    </div>
  );
}
