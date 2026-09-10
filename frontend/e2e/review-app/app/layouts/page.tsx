"use client";

import { useEffect, useState } from "react";
import { UnifiedDrawerWrapper } from "@/components/unified-drawer-wrapper";
import { useUserUiLayout } from "@/lib/use-user-ui-layout";
import { setFixtureAccount } from "../../clerk";

export default function LayoutFixture() {
  const [open, setOpen] = useState(true);
  const preference = useUserUiLayout(true);
  useEffect(() => {
    setFixtureAccount("alice", "org-a");
  }, []);
  const visibility = (patch: { showTask?: boolean; showTrial?: boolean }) => {
    preference.update(patch);
    void preference.flush();
  };
  return (
    <>
      <button onClick={() => setOpen(true)}>Open drawer</button>
      <div className="fixed bottom-2 left-2 z-50 flex gap-2 bg-white p-2 text-black">
        <button onClick={() => setFixtureAccount("alice", "org-a")}>
          Alice A
        </button>
        <button onClick={() => setFixtureAccount("bob", "org-a")}>Bob A</button>
        <button onClick={() => setFixtureAccount("alice", "org-b")}>
          Alice B
        </button>
        <output data-testid="layout">
          {JSON.stringify(preference.layout)}
        </output>
        <span data-testid="save-status">{preference.status}</span>
      </div>
      {open && (
        <UnifiedDrawerWrapper
          key={preference.identity}
          open={open}
          onOpenChange={setOpen}
          mode="trial"
          layout={preference.layout}
          onLayoutChange={preference.update}
          onLayoutCommit={preference.flush}
          layoutSaveError={preference.status === "error"}
          onRetryLayoutSave={preference.retry}
          showTask={preference.layout.showTask}
          showTrial={preference.layout.showTrial}
          onShowTaskChange={(showTask) => visibility({ showTask })}
          onShowTrialChange={(showTrial) => visibility({ showTrial })}
          sideBySideLeft={<div>Task definition contents</div>}
          taskContent={<div>Task contents</div>}
          renderTrial={(action) => (
            <div className="pt-12">{action}Trial contents</div>
          )}
        />
      )}
    </>
  );
}
