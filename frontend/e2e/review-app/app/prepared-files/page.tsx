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
  const retain = search.get("retain") === "true";
  const [tab, setTab] = useState(search.get("tab") ?? "files");
  const [visited, setVisited] = useState(new Set([tab]));
  function selectTab(next: string) {
    setTab(next);
    setVisited((previous) => new Set(previous).add(next));
  }
  return (
    <>
      <button onClick={() => selectTab("files")}>Show files</button>
      <button onClick={() => selectTab("artifacts")}>Show artifacts</button>
      <div style={{ height: 650 }}>
        {(tab === "files" || (retain && visited.has("files"))) && (
          <div hidden={tab !== "files"} style={{ height: "100%" }}>
            <TaskFilesPanel
              isOpen
              isActive={tab === "files"}
              onClose={() => {}}
              taskId={null}
              filesUrl="/api/trials/prepared-1/files"
              trialAttempt={1}
              initialFilePath={filePath}
              contentOnly
              activePane="file"
            />
          </div>
        )}
        {(tab === "artifacts" || (retain && visited.has("artifacts"))) && (
          <div hidden={tab !== "artifacts"} style={{ height: "100%" }}>
            <ArtifactsViewer
              isActive={tab === "artifacts"}
              filesUrl="/api/trials/prepared-1/files"
              trialAttempt={1}
              initialFilePath={filePath}
            />
          </div>
        )}
      </div>
    </>
  );
}
