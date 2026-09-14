"use client";
import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import { TaskFilesPanel } from "@/components/task-files-panel";
import { ArtifactsViewer } from "@/components/artifacts-viewer";
export default function PreparedFilePage() {
  return (
    <Suspense>
      <PreparedFileFixture />
    </Suspense>
  );
}

function PreparedFileFixture() {
  const search = useSearchParams();
  const filePath = search.get("file") ?? "artifacts/readme.txt";
  const [tab, setTab] = useState("files");
  return (
    <>
      <button onClick={() => setTab("files")}>Show files</button>
      <button onClick={() => setTab("artifacts")}>Show artifacts</button>
      <div style={{ height: 650 }}>
        {tab === "files" ? (
          <TaskFilesPanel
            isOpen
            onClose={() => {}}
            taskId={null}
            filesUrl="/api/trials/prepared-1/files"
            trialAttempt={1}
            initialFilePath={filePath}
            contentOnly
            activePane="file"
          />
        ) : (
          <ArtifactsViewer
            filesUrl="/api/trials/prepared-1/files"
            trialAttempt={1}
            initialFilePath={filePath}
          />
        )}
      </div>
    </>
  );
}
