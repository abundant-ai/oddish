"use client";
import { useState } from "react";
import { useTaskFileTree } from "@/lib/use-task-file-tree";
import { setFixtureAccount } from "../../clerk";
export default function CacheFixture() {
  const [version, setVersion] = useState(7);
  const tree = useTaskFileTree({
    enabled: true,
    url: "/api/tasks/task-a/files",
    version,
    hash: null,
  });
  return (
    <>
      <button onClick={() => setFixtureAccount("alice", "org-a")}>
        Alice A
      </button>
      <button onClick={() => setFixtureAccount("bob", "org-a")}>Bob A</button>
      <button onClick={() => setFixtureAccount("alice", "org-b")}>
        Alice B
      </button>
      <button onClick={() => setVersion(6)}>Version 6</button>
      <button onClick={() => setVersion(7)}>Version 7</button>
      <button onClick={() => void tree.loadDirectory("tests", "page-2")}>
        More tests
      </button>
      <output data-testid="tree">{JSON.stringify(tree.data ?? null)}</output>
    </>
  );
}
