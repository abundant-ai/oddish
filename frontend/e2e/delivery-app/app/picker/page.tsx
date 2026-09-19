"use client";

import { StrictMode, Suspense, useState } from "react";
import { TestAuthProvider } from "../../clerk";
import { SWRConfig } from "swr";
import TasksPage from "../../../../src/app/(app)/tasks/page";

export default function Picker() {
  const [generation, setGeneration] = useState(0);
  const [orgId, setOrgId] = useState("org-1");
  return (
    <StrictMode>
      <Suspense>
        <SWRConfig value={{ provider: () => new Map() }}>
          <button onClick={() => setGeneration((value) => value + 1)}>
            Remount picker
          </button>
          <button
            onClick={() => setOrgId(orgId === "org-1" ? "org-2" : "org-1")}
          >
            Switch organization
          </button>
          <TestAuthProvider
            value={{
              orgRole: "org:admin",
              isLoaded: true,
              userId: "user-1",
              orgId,
            }}
          >
            <TasksPage key={generation} />
          </TestAuthProvider>
        </SWRConfig>
      </Suspense>
    </StrictMode>
  );
}
