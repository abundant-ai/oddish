"use client";
import { useState } from "react";
import { TaskFilesPanel } from "@/components/task-files-panel";
import { ArtifactsViewer } from "@/components/artifacts-viewer";
export default function PreparedFileFixture() {
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
            initialFilePath="artifacts/readme.txt"
            contentOnly
            activePane="file"
          />
        ) : (
          <ArtifactsViewer
            filesUrl="/api/trials/prepared-1/files"
            trialAttempt={1}
            initialFilePath="artifacts/readme.txt"
          />
        )}
      </div>
    </>
  );
}
